import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None, timeout: float = 10) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db(db_path: str | None = None, timeout: float = 10):
    conn = connect(db_path, timeout)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def immediate_tx(db_path: str | None = None, timeout: float = 10):
    """BEGIN IMMEDIATE 事务：进入即拿写锁，供续灌/整窗重放互斥使用。

    业务异常（ServiceError）表示带白话错误的正常收口（如失败闭），
    收口改动照常提交后再把异常抛给调用方；其他异常才回滚。
    """
    from app.services.errors import ServiceError  # 局部导入避免循环依赖

    conn = connect(db_path, timeout)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            # 别的调度正持有库写锁：翻译成白话 409，不抛裸 500
            raise ServiceError("数据库正被别的调度写入，抢锁失败，请稍后再试", 409) from exc
        yield conn
        conn.commit()
    except ServiceError:
        conn.commit()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# 窗口状态 CHECK 已含「失败闭」；切片状态沿用「待重放/已落库/失败」。
SCHEMA = """
CREATE TABLE IF NOT EXISTS backfill_windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('草稿','已计划','执行中','失败闭','已完成')),
  fail_armed INTEGER NOT NULL DEFAULT 0,
  armed_slice_id INTEGER,
  closed_at TEXT,
  resume_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS backfill_slices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL REFERENCES backfill_windows(id),
  slice_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待重放','已落库','失败')),
  row_count INTEGER NOT NULL DEFAULT 0,
  error_text TEXT,
  UNIQUE(window_id, slice_date)
);

CREATE TABLE IF NOT EXISTS backfill_archives (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL,
  slice_id INTEGER NOT NULL,
  slice_date TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  retention_no TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS backfill_locks (
  window_id INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT 'window',
  holder TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (window_id, kind)
);

CREATE TABLE IF NOT EXISTS coverage_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL,
  planned INTEGER NOT NULL,
  loaded INTEGER NOT NULL,
  missing INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# 旧版窗口表（无失败闭 CHECK、无新列）的重建 DDL
_WINDOWS_NEW = """
CREATE TABLE backfill_windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('草稿','已计划','执行中','失败闭','已完成')),
  fail_armed INTEGER NOT NULL DEFAULT 0,
  armed_slice_id INTEGER,
  closed_at TEXT,
  resume_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


def _columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn) -> None:
    """对旧库幂等补列、升级窗口表 CHECK 约束。"""
    win_cols = _columns(conn, "backfill_windows")
    for col, ddl in (
        ("fail_armed", "ALTER TABLE backfill_windows ADD COLUMN fail_armed INTEGER NOT NULL DEFAULT 0"),
        ("armed_slice_id", "ALTER TABLE backfill_windows ADD COLUMN armed_slice_id INTEGER"),
        ("closed_at", "ALTER TABLE backfill_windows ADD COLUMN closed_at TEXT"),
        ("resume_count", "ALTER TABLE backfill_windows ADD COLUMN resume_count INTEGER NOT NULL DEFAULT 0"),
    ):
        if col not in win_cols:
            conn.execute(ddl)

    if "error_text" not in _columns(conn, "backfill_slices"):
        conn.execute("ALTER TABLE backfill_slices ADD COLUMN error_text TEXT")

    ddl_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='backfill_windows'"
    ).fetchone()
    if ddl_row and "失败闭" not in (ddl_row["sql"] or ""):
        # SQLite 不能直接改 CHECK，走官方表重建流程
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("ALTER TABLE backfill_windows RENAME TO backfill_windows_old")
        conn.execute(_WINDOWS_NEW)
        conn.execute(
            "INSERT INTO backfill_windows"
            "(id, table_name, start_date, end_date, status, created_at, "
            "fail_armed, armed_slice_id, closed_at, resume_count) "
            "SELECT id, table_name, start_date, end_date, status, created_at, 0, NULL, NULL, 0 "
            "FROM backfill_windows_old"
        )
        conn.execute("DROP TABLE backfill_windows_old")
        conn.execute("PRAGMA foreign_keys=ON")


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        if conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone():
            return
        conn.execute(
            "INSERT INTO backfill_windows(table_name, start_date, end_date, status) "
            "VALUES ('dw_orders_d','2026-11-10','2026-11-12','草稿')"
        )
        conn.execute(
            "INSERT INTO backfill_windows(table_name, start_date, end_date, status) "
            "VALUES ('dw_payments_d','2026-11-11','2026-11-11','草稿')"
        )
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
