"""发起人/审核员同时操作的并发互斥（2026-08-31，三层修复）：

- enqueue_regen 状态原子翻转：只有一方获得重跑权，另一方 merged 不重复入队
- 在跑（processing）任务的状态不被驳回/修正打扰
- 调度器按任务去重：同一任务重复入队第二次返回 False
"""
import pytest
from unittest.mock import AsyncMock, patch

from sqlalchemy import text

from src.db.session import SessionLocal
from src.services.regen import enqueue_regen


async def _make_task(status="review", query="并发测试"):
    import uuid as _uuid
    from src.models.tasks import Task
    async with SessionLocal() as session:
        task = Task(idempotency_key=f"k-{_uuid.uuid4().hex[:12]}",
                    query=f"{query}-{_uuid.uuid4().hex[:6]}",
                    content_type="generic", mode="general", status=status)
        session.add(task)
        await session.commit()
        return task.id


@pytest.mark.asyncio
async def test_second_regen_call_merges():
    """双方同时提交：先者获得重跑权，后者合并（不重复清理/入队）。"""
    tid = await _make_task("review")
    with patch("src.stream.scheduler.scheduler.enqueue",
               new=AsyncMock(return_value=True)):
        first = await enqueue_regen(tid)
        second = await enqueue_regen(tid)
    assert first["kind"] == "pipeline"      # 无标记 → 整体重生成
    assert second["kind"] == "merged"       # 状态已翻成 draft，第二方合并


@pytest.mark.asyncio
async def test_processing_task_not_flipped():
    """在跑任务：驳回/修正不翻转状态、不入队，返回 merged。"""
    tid = await _make_task("processing")
    r = await enqueue_regen(tid)
    assert r["kind"] == "merged"
    async with SessionLocal() as session:
        st = (await session.execute(text(
            "SELECT status FROM tasks WHERE id = :i"), {"i": tid})).scalar()
    assert st == "processing"


@pytest.mark.asyncio
async def test_scheduler_dedupes_same_task():
    """调度器去重双保险：同一任务在队/在跑时重复入队被拒。"""
    from src.stream.scheduler import TaskScheduler
    sch = TaskScheduler()
    assert await sch.enqueue("t-1", "q") is True
    assert await sch.enqueue("t-1", "q") is False      # 在队去重
    sch._meta["t-1"]["status"] = "processing"
    assert await sch.enqueue("t-1", "q") is False      # 在跑去重
    assert await sch.enqueue("t-2", "q") is True       # 不同任务不受影响
