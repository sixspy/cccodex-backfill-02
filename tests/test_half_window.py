"""半窗收口（失败片冻结 + 留存归档路线）与续灌纠偏验收。

验收两步连跑：失败闭签发 → 续灌纠偏，结束后进度目录不得再半绿半空。
"""

import threading
import time

import pytest

from app.db import get_db, init_db
from app.services.errors import ServiceError
from app.services.windows import (
    arm_failure,
    coverage_report,
    list_windows,
    plan_slices,
    replay_window,
    resume_window,
    window_detail,
)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "half.db")
    init_db(path)
    plan = plan_slices(1, path)  # 种子窗口#1：dw_orders_d，3 片
    assert len(plan["slices"]) == 3
    return path


def _armed_window(db_path, window_id=1):
    """挂闸中间片并整窗重放，返回失败闭后的详情。"""
    slices = window_detail(window_id, db_path)["slices"]
    arm_failure(window_id, slices[1]["id"], db_path)
    with pytest.raises(ServiceError) as ei:
        replay_window(window_id, db_path)
    assert "失败闭" in ei.value.message
    return window_detail(window_id, db_path)


# —— 第一步：失败闭签发 ——

@pytest.mark.asyncio
async def test_failure_closure_freezes_and_archives(db_path):
    detail = _armed_window(db_path)
    # 窗态
    assert detail["status"] == "失败闭"
    assert detail["resumable"] is True
    statuses = [(s["slice_date"], s["status"]) for s in detail["slices"]]
    assert statuses == [
        ("2026-11-10", "已落库"),
        ("2026-11-11", "失败"),
        ("2026-11-12", "待重放"),
    ]
    # 失败片冻结，带白话错误
    failed = next(s for s in detail["slices"] if s["status"] == "失败")
    assert failed["error_text"]
    # 已成片留存入历史档并签发留存号，且仍占落点（片未被删/回滚）
    assert detail["retention_no"] == "RC-1-1"
    assert {a["slice_date"] for a in detail["archives"]} == {"2026-11-10"}
    assert all(a["retention_no"] == "RC-1-1" for a in detail["archives"])
    # 列表视图同口径亮失败闭标记 + 留存号
    win = next(w for w in list_windows(db_path) if w["id"] == 1)
    assert win["status"] == "失败闭" and win["resumable"] is True
    assert win["retention_no"] == "RC-1-1"
    # 锁已释放
    assert detail["lock"] is None


@pytest.mark.asyncio
async def test_coverage_half_green_then_aligned(db_path):
    _armed_window(db_path)
    half = coverage_report(1, db_path)
    assert half["planned"] == 3 and half["loaded"] == 1 and half["missing"] == 2
    assert "半绿半空" in half["summary"]

    # —— 第二步：续灌纠偏 ——
    fixed = resume_window(1, db_path)
    assert fixed["status"] == "已完成"
    assert fixed["resumable"] is False
    assert all(s["status"] == "已落库" for s in fixed["slices"])
    assert fixed["resume_count"] == 1

    full = coverage_report(1, db_path)
    assert full["missing"] == 0 and full["loaded"] == 3
    assert "片界全齐" in full["summary"]
    # 进度目录里不再有失败/待重放片
    assert fixed["failed_count"] == 0 and fixed["pending_count"] == 0


@pytest.mark.asyncio
async def test_resume_rejects_non_failure_closed(db_path):
    # 已计划窗口不可续灌——窗态与可灌判定同口径，偏了就拒
    with pytest.raises(ServiceError) as ei:
        resume_window(1, db_path)
    assert ei.value.status_code == 409
    win = next(w for w in list_windows(db_path) if w["id"] == 1)
    assert win["resumable"] is False


@pytest.mark.asyncio
async def test_single_replay_on_closed_window_rejected(db_path):
    _armed_window(db_path)
    failed_id = next(s["id"] for s in window_detail(1, db_path)["slices"] if s["status"] == "失败")
    with pytest.raises(ServiceError) as ei:
        # 即便有人直接打单片重放接口，也被同口径挡住
        from app.services.windows import replay_slice
        replay_slice(failed_id, db_path)
    assert ei.value.status_code == 409


@pytest.mark.asyncio
async def test_archives_not_duplicated_on_resume(db_path):
    _armed_window(db_path)
    before = len(window_detail(1, db_path)["archives"])
    resume_window(1, db_path)
    after = window_detail(1, db_path)["archives"]
    # 续灌只补失败片，不重复归档已留存的片
    assert len(after) == before == 1


@pytest.mark.asyncio
async def test_resume_fail_again_appends_new_retention(db_path):
    # 第一次失败闭
    detail = _armed_window(db_path)
    assert detail["retention_no"] == "RC-1-1"
    # 续灌前再次给最后一片挂闸，续灌会再败
    last = next(s for s in detail["slices"] if s["status"] == "待重放")
    arm_failure(1, last["id"], db_path)
    with pytest.raises(ServiceError) as ei:
        resume_window(1, db_path)
    assert "失败闭" in ei.value.message
    again = window_detail(1, db_path)
    assert again["status"] == "失败闭"
    assert again["retention_no"] == "RC-1-2"
    # 两批留存都在历史档，已成片始终占落点
    retentions = {a["retention_no"] for a in again["archives"]}
    assert retentions == {"RC-1-1", "RC-1-2"}


def test_two_schedulers_race_resume_only_one_wins(db_path):
    """两个调度同抢续灌：缺锁会互相覆盖片指针；有锁只允许一个成功。"""
    import app.services.windows as svc

    _armed_window(db_path)
    barrier = threading.Barrier(2)
    results = []

    def pre_hook(holder):
        barrier.wait()  # 两调度在进入写事务前先汇合

    def locked_hook(holder):
        time.sleep(0.6)  # 赢家拿锁后多占写锁，逼输家在 BEGIN IMMEDIATE 上超时

    def worker(holder):
        try:
            results.append((holder, "ok", resume_window(1, db_path, holder=holder, timeout=0.3)))
        except ServiceError as exc:
            results.append((holder, "err", exc.message))

    old_hook, old_locked = svc._resume_hook, svc._resume_locked
    svc._resume_hook, svc._resume_locked = pre_hook, locked_hook
    try:
        t1 = threading.Thread(target=worker, args=("调度甲",))
        t2 = threading.Thread(target=worker, args=("调度乙",))
        t1.start(); t2.start(); t1.join(); t2.join()
    finally:
        svc._resume_hook, svc._resume_locked = old_hook, old_locked

    oks = [r for r in results if r[1] == "ok"]
    errs = [r for r in results if r[1] == "err"]
    assert len(oks) == 1 and len(errs) == 1
    assert "占用" in errs[0][2] or "抢锁" in errs[0][2]

    final = window_detail(1, db_path)
    # 没被踩坏：全部片只落一次，无残留锁
    assert all(s["status"] == "已落库" for s in final["slices"])
    assert final["status"] == "已完成"
    assert final["lock"] is None
    with get_db(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM backfill_locks WHERE window_id=1"
        ).fetchone()["c"] == 0


def test_loaded_slices_keep_target_slot_after_closure(db_path):
    """半窗行仍占落点：失败闭不回滚已成片。"""
    _armed_window(db_path)
    with get_db(db_path) as conn:
        kept = conn.execute(
            "SELECT slice_date, row_count FROM backfill_slices "
            "WHERE window_id=1 AND status='已落库'"
        ).fetchall()
    assert [(r["slice_date"], r["row_count"] > 0) for r in kept] == [("2026-11-10", True)]
