"""回灌窗口、切片重放、半窗收口与续灌纠偏。

半窗收口只走一条路线（不与整窗回滚路线并存）：
失败片冻结在「失败」态，已成片不回滚、继续占据目标表落点，
并把已成片部分写入历史档（backfill_archives）签发留存号；窗口置「失败闭」。
"""

from datetime import date, timedelta

from app.db import get_db, immediate_tx
from app.services.errors import ServiceError

# 失败闭窗口可续灌；其余状态不可。前后端与各接口共用这一口径。
RESUMABLE_STATUS = "失败闭"
ACTIVE_STATUSES = ("已计划", "执行中")

# 测试钩子：
# _resume_hook   —— 续灌进入写事务前回调（汇合两个调度）
# _resume_locked —— 续灌拿到窗口锁后回调（把赢家写事务时间窗拉长）
_resume_hook = None
_resume_locked = None


def _daterange(start: str, end: str) -> list[str]:
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    if e < s:
        raise ServiceError("结束日不能早于开始日")
    out = []
    cur = s
    while cur <= e:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _retention_no(window_id: int, conn) -> str:
    seq = (
        conn.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 AS n FROM backfill_archives WHERE window_id=?",
            (window_id,),
        ).fetchone()["n"]
    )
    return f"RC-{window_id}-{seq}"


def _archive_loaded(window_id: int, retention_no: str, conn) -> int:
    """把本窗口当前所有「已落库」片存入历史档（已成片仍占落点，不删片不回滚）。"""
    rows = conn.execute(
        "SELECT id, slice_date, row_count FROM backfill_slices "
        "WHERE window_id=? AND status='已落库' ORDER BY slice_date",
        (window_id,),
    ).fetchall()
    for r in rows:
        conn.execute(
            "INSERT INTO backfill_archives"
            "(window_id, slice_id, slice_date, row_count, retention_no) "
            "VALUES (?,?,?,?,?)",
            (window_id, r["id"], r["slice_date"], r["row_count"], retention_no),
        )
    return len(rows)


def _acquire_lock(conn, window_id: int, holder: str) -> None:
    try:
        conn.execute(
            "INSERT INTO backfill_locks(window_id, kind, holder) VALUES (?, 'window', ?)",
            (window_id, holder),
        )
    except Exception as exc:
        row = conn.execute(
            "SELECT holder FROM backfill_locks WHERE window_id=? AND kind='window'",
            (window_id,),
        ).fetchone()
        who = row["holder"] if row else "另一调度"
        raise ServiceError(f"窗口#{window_id} 正被{who}占用，请稍后再试，别和别的调度抢", 409) from exc


def _release_lock(conn, window_id: int) -> None:
    conn.execute(
        "DELETE FROM backfill_locks WHERE window_id=? AND kind='window'", (window_id,)
    )


def _replay_one(sl, conn) -> dict:
    """单条切片落库；挂闸片模拟重放故障，抛白话错误。"""
    if sl["status"] == "已落库":
        raise ServiceError("切片已落库，不能重复重放", 409)
    if sl["status"] != "待重放":
        raise ServiceError("仅待重放切片可重放", 409)
    win = conn.execute(
        "SELECT * FROM backfill_windows WHERE id=?", (sl["window_id"],)
    ).fetchone()
    if win and win["armed_slice_id"] == sl["id"]:
        conn.execute(
            "UPDATE backfill_slices SET status='失败', error_text=? WHERE id=?",
            ("重放时数据源缺分区，落库失败", sl["id"]),
        )
        conn.execute(
            "UPDATE backfill_windows SET status='执行中' WHERE id=?",
            (sl["window_id"],),
        )
        raise ServiceError(f"切片 {sl['slice_date']} 重放出错：数据源缺分区，没写进目标表", 409)
    rows = 100 + int(sl["slice_date"].replace("-", "")) % 97
    conn.execute(
        "UPDATE backfill_slices SET status='已落库', row_count=?, error_text=NULL WHERE id=?",
        (rows, sl["id"]),
    )
    return dict(
        conn.execute("SELECT * FROM backfill_slices WHERE id=?", (sl["id"],)).fetchone()
    )


def _win_payload(win, conn) -> dict:
    """统一的窗口视图：窗态与可灌判定同口径产出 resumable。"""
    item = dict(win)
    counts = conn.execute(
        "SELECT "
        "SUM(CASE WHEN status='已落库' THEN 1 ELSE 0 END) AS loaded, "
        "SUM(CASE WHEN status='失败' THEN 1 ELSE 0 END) AS failed, "
        "SUM(CASE WHEN status='待重放' THEN 1 ELSE 0 END) AS pending, "
        "COUNT(*) AS total FROM backfill_slices WHERE window_id=?",
        (win["id"],),
    ).fetchone()
    item["loaded_count"] = counts["loaded"] or 0
    item["failed_count"] = counts["failed"] or 0
    item["pending_count"] = counts["pending"] or 0
    item["slice_count"] = counts["total"] or 0
    item["resumable"] = item["status"] == RESUMABLE_STATUS
    latest = conn.execute(
        "SELECT retention_no FROM backfill_archives WHERE window_id=? "
        "ORDER BY id DESC LIMIT 1",
        (win["id"],),
    ).fetchone()
    item["retention_no"] = latest["retention_no"] if latest else None
    item["locked"] = bool(
        conn.execute(
            "SELECT 1 FROM backfill_locks WHERE window_id=? AND kind='window'",
            (win["id"],),
        ).fetchone()
    )
    return item


def list_windows(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        wins = conn.execute("SELECT * FROM backfill_windows ORDER BY id").fetchall()
        return [_win_payload(w, conn) for w in wins]


def _detail_payload(conn, window_id: int) -> dict:
    win = conn.execute(
        "SELECT * FROM backfill_windows WHERE id=?", (window_id,)
    ).fetchone()
    if not win:
        raise ServiceError("回灌窗口不存在", 404)
    slices = conn.execute(
        "SELECT * FROM backfill_slices WHERE window_id=? ORDER BY slice_date",
        (window_id,),
    ).fetchall()
    archives = conn.execute(
        "SELECT * FROM backfill_archives WHERE window_id=? ORDER BY id",
        (window_id,),
    ).fetchall()
    lock = conn.execute(
        "SELECT holder, created_at FROM backfill_locks WHERE window_id=? AND kind='window'",
        (window_id,),
    ).fetchone()
    payload = _win_payload(win, conn)
    payload["slices"] = [dict(s) for s in slices]
    payload["archives"] = [dict(a) for a in archives]
    payload["lock"] = dict(lock) if lock else None
    return payload


def window_detail(window_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        return _detail_payload(conn, window_id)


def plan_slices(window_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        win = conn.execute(
            "SELECT * FROM backfill_windows WHERE id=?", (window_id,)
        ).fetchone()
        if not win:
            raise ServiceError("回灌窗口不存在", 404)
        if win["status"] not in ("草稿", "已计划"):
            raise ServiceError("当前状态不可再生成切片", 409)
        days = _daterange(win["start_date"], win["end_date"])
        conn.execute("DELETE FROM backfill_slices WHERE window_id=?", (window_id,))
        for d in days:
            conn.execute(
                "INSERT INTO backfill_slices(window_id, slice_date, status, row_count) "
                "VALUES (?,?, '待重放', 0)",
                (window_id, d),
            )
        conn.execute(
            "UPDATE backfill_windows SET status='已计划', fail_armed=0, armed_slice_id=NULL "
            "WHERE id=?",
            (window_id,),
        )
        slices = conn.execute(
            "SELECT * FROM backfill_slices WHERE window_id=? ORDER BY slice_date",
            (window_id,),
        ).fetchall()
        return {"window_id": window_id, "slices": [dict(s) for s in slices]}


def list_slices(window_id: int, db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM backfill_slices WHERE window_id=? ORDER BY slice_date",
            (window_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def replay_slice(slice_id: int, db_path: str | None = None, holder: str = "仓管") -> dict:
    """单片重放；与整窗重放/续灌共用窗口锁，避免并发互踩片指针。"""
    with immediate_tx(db_path) as conn:
        sl = conn.execute(
            "SELECT * FROM backfill_slices WHERE id=?", (slice_id,)
        ).fetchone()
        if not sl:
            raise ServiceError("切片不存在", 404)
        window_id = sl["window_id"]
        win = conn.execute(
            "SELECT * FROM backfill_windows WHERE id=?", (window_id,)
        ).fetchone()
        if win["status"] == RESUMABLE_STATUS:
            raise ServiceError("窗口已是失败闭，单片不能再动，请走续灌纠偏", 409)
        _acquire_lock(conn, window_id, holder)
        try:
            try:
                row = _replay_one(sl, conn)
            except ServiceError:
                # 单片故障即半窗收口：冻结失败片、留存已成片、挂失败闭
                conn.execute(
                    "UPDATE backfill_windows SET status='失败闭', closed_at=datetime('now') "
                    "WHERE id=?",
                    (window_id,),
                )
                _freeze_and_archive(conn, window_id)
                raise
            pending = conn.execute(
                "SELECT COUNT(*) AS c FROM backfill_slices "
                "WHERE window_id=? AND status!='已落库'",
                (window_id,),
            ).fetchone()["c"]
            conn.execute(
                "UPDATE backfill_windows SET status=? WHERE id=?",
                ("已完成" if pending == 0 else "执行中", window_id),
            )
            return row
        finally:
            _release_lock(conn, window_id)


def _freeze_and_archive(conn, window_id: int) -> dict:
    """半窗收口（唯一路线）：冻失败片 + 已成片留存入历史档 + 签发留存号。"""
    failed = conn.execute(
        "SELECT id FROM backfill_slices WHERE window_id=? AND status='失败'", (window_id,)
    ).fetchall()
    retention_no = _retention_no(window_id, conn)
    archived = _archive_loaded(window_id, retention_no, conn)
    conn.execute(
        "UPDATE backfill_windows SET fail_armed=0, armed_slice_id=NULL WHERE id=?",
        (window_id,),
    )
    return {
        "retention_no": retention_no,
        "archived_count": archived,
        "failed_slice_ids": [r["id"] for r in failed],
    }


def arm_failure(
    window_id: int, slice_id: int, db_path: str | None = None
) -> dict:
    """挂闸：指定下一片重放必失败，用于签发失败闭。"""
    with get_db(db_path) as conn:
        win = conn.execute(
            "SELECT * FROM backfill_windows WHERE id=?", (window_id,)
        ).fetchone()
        if not win:
            raise ServiceError("回灌窗口不存在", 404)
        if win["status"] not in ACTIVE_STATUSES + (RESUMABLE_STATUS,):
            raise ServiceError("只有计划/执行中/失败闭的窗口能挂闸模拟故障", 409)
        sl = conn.execute(
            "SELECT * FROM backfill_slices WHERE id=? AND window_id=?",
            (slice_id, window_id),
        ).fetchone()
        if not sl:
            raise ServiceError("切片不属于这个窗口", 404)
        if sl["status"] != "待重放":
            raise ServiceError("只有待重放片能挂闸", 409)
        conn.execute(
            "UPDATE backfill_windows SET fail_armed=1, armed_slice_id=? WHERE id=?",
            (slice_id, window_id),
        )
        return {"window_id": window_id, "armed_slice_id": slice_id}


def replay_window(
    window_id: int, db_path: str | None = None, holder: str = "仓管"
) -> dict:
    """整窗重放：逐片落库，中途出错当场半窗收口并抛白话错误。"""
    with immediate_tx(db_path) as conn:
        win = conn.execute(
            "SELECT * FROM backfill_windows WHERE id=?", (window_id,)
        ).fetchone()
        if not win:
            raise ServiceError("回灌窗口不存在", 404)
        if win["status"] == RESUMABLE_STATUS:
            raise ServiceError("窗口已是失败闭，别直接重放，请走续灌纠偏", 409)
        if win["status"] not in ACTIVE_STATUSES:
            raise ServiceError("当前窗口状态不能整窗重放", 409)
        _acquire_lock(conn, window_id, holder)
        try:
            slices = conn.execute(
                "SELECT * FROM backfill_slices WHERE window_id=? ORDER BY slice_date",
                (window_id,),
            ).fetchall()
            try:
                for sl in slices:
                    if sl["status"] != "待重放":  # 已落库跳过；失败片不会出现在可重放窗口
                        continue
                    _replay_one(sl, conn)
            except ServiceError as exc:
                conn.execute(
                    "UPDATE backfill_windows SET status='失败闭', closed_at=datetime('now') "
                    "WHERE id=?",
                    (window_id,),
                )
                closure = _freeze_and_archive(conn, window_id)
                raise ServiceError(
                    f"整窗重放中途出错，已挂失败闭；{exc.message}；"
                    f"已成片留存号 {closure['retention_no']}，可续灌纠偏",
                    409,
                ) from exc
            conn.execute(
                "UPDATE backfill_windows SET status='已完成' WHERE id=?", (window_id,)
            )
            return _detail_payload(conn, window_id)
        finally:
            _release_lock(conn, window_id)


def resume_window(
    window_id: int, db_path: str | None = None, holder: str = "仓管",
    timeout: float = 10,
) -> dict:
    """续灌纠偏：只认失败闭；抢窗口锁，冻结片回待重放后逐片补灌。"""
    if _resume_hook is not None:
        _resume_hook(holder)
    with immediate_tx(db_path, timeout=timeout) as conn:
        win = conn.execute(
            "SELECT * FROM backfill_windows WHERE id=?", (window_id,)
        ).fetchone()
        if not win:
            raise ServiceError("回灌窗口不存在", 404)
        # 先抢窗口锁，再在锁内做可灌判定：两个调度同抢时统一在锁上见输赢
        _acquire_lock(conn, window_id, holder)
        if _resume_locked is not None:
            _resume_locked(holder)
        try:
            win = conn.execute(
                "SELECT * FROM backfill_windows WHERE id=?", (window_id,)
            ).fetchone()
            # 窗态与可灌判定同口径：不是失败闭一律拒
            if win["status"] != RESUMABLE_STATUS:
                raise ServiceError("只有失败闭窗口能续灌，当前状态不允许", 409)
            conn.execute(
                "UPDATE backfill_slices SET status='待重放', row_count=0, error_text=NULL "
                "WHERE window_id=? AND status='失败'",
                (window_id,),
            )
            conn.execute(
                "UPDATE backfill_windows SET status='执行中', "
                "resume_count=resume_count+1 WHERE id=?",
                (window_id,),
            )
            slices = conn.execute(
                "SELECT * FROM backfill_slices WHERE window_id=? ORDER BY slice_date",
                (window_id,),
            ).fetchall()
            try:
                for sl in slices:
                    if sl["status"] == "待重放":
                        _replay_one(sl, conn)
            except ServiceError as exc:
                conn.execute(
                    "UPDATE backfill_windows SET status='失败闭', closed_at=datetime('now') "
                    "WHERE id=?",
                    (window_id,),
                )
                closure = _freeze_and_archive(conn, window_id)
                raise ServiceError(
                    f"续灌又出错，已重新挂失败闭；{exc.message}；"
                    f"新留存号 {closure['retention_no']}",
                    409,
                ) from exc
            conn.execute(
                "UPDATE backfill_windows SET status='已完成', closed_at=NULL WHERE id=?",
                (window_id,),
            )
            return _detail_payload(conn, window_id)
        finally:
            _release_lock(conn, window_id)


def coverage_report(window_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        win = conn.execute(
            "SELECT * FROM backfill_windows WHERE id=?", (window_id,)
        ).fetchone()
        if not win:
            raise ServiceError("回灌窗口不存在", 404)
        stats = conn.execute(
            "SELECT "
            "COUNT(*) AS planned, "
            "SUM(CASE WHEN status='已落库' THEN 1 ELSE 0 END) AS loaded, "
            "SUM(CASE WHEN status='失败' THEN 1 ELSE 0 END) AS failed, "
            "SUM(CASE WHEN status='待重放' THEN 1 ELSE 0 END) AS pending "
            "FROM backfill_slices WHERE window_id=?",
            (window_id,),
        ).fetchone()
        planned = stats["planned"]
        loaded = stats["loaded"] or 0
        failed = stats["failed"] or 0
        pending = stats["pending"] or 0
        missing = planned - loaded
        if win["status"] == RESUMABLE_STATUS:
            tail = f"；窗口失败闭（失败{failed}片、待重放{pending}片），半绿半空，先续灌再对账"
        elif missing == 0:
            tail = "；片界全齐，对账通过"
        else:
            tail = f"；尚有缺口{missing}片"
        summary = (
            f"窗口#{window_id} {win['table_name']}：计划{planned}片，已落库{loaded}，"
            f"缺口{missing}{tail}"
        )
        cur = conn.execute(
            "INSERT INTO coverage_reports(window_id, planned, loaded, missing, summary) "
            "VALUES (?,?,?,?,?)",
            (window_id, planned, loaded, missing, summary),
        )
        row = conn.execute(
            "SELECT * FROM coverage_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)


def list_coverage(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM coverage_reports ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
