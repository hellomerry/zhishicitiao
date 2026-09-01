import uuid
import pytest
from unittest.mock import patch
from sqlalchemy import select, func
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.models.drafts import Draft
from src.pipeline.nodes import node_draft_gen, node_draft_polish

FAKE_DRAFT = {"text": "这是测试正文。" * 60, "model_version": "deepseek/deepseek-v4-pro",
              "cost_cny": 0.01, "degraded": False}
FAKE_POLISHED = {"text": "校稿后的正文。" * 60, "model_version": "deepseek/deepseek-v4-pro",
                 "cost_cny": 0.01, "degraded": False}


async def _make_task_with_draft():
    async with SessionLocal() as session:
        task = Task(idempotency_key=f"p-{uuid.uuid4().hex[:8]}", query="测试词条",
                    content_type="general", mode="general")
        session.add(task)
        await session.commit()
        await session.refresh(task)
        tid = task.id
    with patch("src.pipeline.nodes.call_with_failover", return_value=FAKE_DRAFT):
        await node_draft_gen({"task_id": tid})
    return tid


@pytest.mark.asyncio
async def test_draft_polish_stores_new_version():
    """校稿成功：存 version+1 的新草稿，prompt_version 标记为 draft_polish_v1。"""
    tid = await _make_task_with_draft()
    with patch("src.pipeline.nodes.call_with_failover", return_value=FAKE_POLISHED):
        out = await node_draft_polish({"task_id": tid})
    assert out["polished"] is True
    assert out["prompt_version"] == "draft_polish_v1"
    async with SessionLocal() as session:
        drafts = (await session.execute(
            select(Draft).where(Draft.task_id == tid)
            .order_by(Draft.version))).scalars().all()
    assert len(drafts) == 2
    assert drafts[-1].version == drafts[0].version + 1
    assert drafts[-1].body == FAKE_POLISHED["text"]
    assert drafts[-1].prompt_version == "draft_polish_v1"


@pytest.mark.asyncio
async def test_draft_polish_llm_failure_not_blocking():
    """校稿 LLM 失败：polished=False，不新增草稿、不抛异常，流水线可继续。"""
    tid = await _make_task_with_draft()
    with patch("src.pipeline.nodes.call_with_failover",
               side_effect=RuntimeError("llm down")):
        out = await node_draft_polish({"task_id": tid})
    assert out["polished"] is False
    async with SessionLocal() as session:
        n = (await session.execute(
            select(func.count()).select_from(Draft).where(
                Draft.task_id == tid))).scalar()
    assert n == 1  # 仍只有 draft_gen 那一版


@pytest.mark.asyncio
async def test_draft_polish_short_output_not_blocking():
    """校稿输出异常短（<100 字）：视为失败，沿用原稿。"""
    tid = await _make_task_with_draft()
    short = dict(FAKE_POLISHED, text="太短")
    with patch("src.pipeline.nodes.call_with_failover", return_value=short):
        out = await node_draft_polish({"task_id": tid})
    assert out["polished"] is False
    async with SessionLocal() as session:
        n = (await session.execute(
            select(func.count()).select_from(Draft).where(
                Draft.task_id == tid))).scalar()
    assert n == 1
