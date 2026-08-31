"""分页画面主体提取（src/services/page_subject.py）+ asset_gen 注入：

- LLM mock 成功：返回 6 个画面主体
- 解析失败 / 数量不等于 6 / LLM 失败 → 回退 None
- asset_gen 节点：提取成功后提示词注入动态主体句并落 tasks.page_subjects；
  提取失败时提示词沿用通用锚定条款、不阻塞出图
"""
import uuid
import pytest
from unittest.mock import AsyncMock, patch
from sqlalchemy import select
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.models.drafts import PageCopy
from src.pipeline.nodes import node_asset_gen
from src.services.page_subject import extract_page_subjects

FAKE_IMAGE = {"hash": "abc", "image_url": "https://example.com/i.png", "model_version": "gpt-image-1.5"}
SUBJECTS = [f"主体{i}" for i in range(1, 7)]
BODIES = [f"第{i}页文案" for i in range(1, 7)]


def _llm_returning(text):
    async def _call(prompt):
        return {"text": text, "model_version": "mock", "cost_cny": 0}
    return _call


def _llm_raising(prompt):
    raise RuntimeError("LLM down")


@pytest.mark.asyncio
async def test_extract_success_returns_six_subjects():
    import json
    r = await extract_page_subjects(BODIES, llm_call=_llm_returning(json.dumps(SUBJECTS, ensure_ascii=False)))
    assert r == SUBJECTS


@pytest.mark.asyncio
async def test_extract_parse_failure_returns_none():
    r = await extract_page_subjects(BODIES, llm_call=_llm_returning("这不是 JSON"))
    assert r is None


@pytest.mark.asyncio
async def test_extract_wrong_count_returns_none():
    import json
    r = await extract_page_subjects(BODIES, llm_call=_llm_returning(json.dumps(SUBJECTS[:5], ensure_ascii=False)))
    assert r is None


@pytest.mark.asyncio
async def test_extract_llm_failure_returns_none():
    r = await extract_page_subjects(BODIES, llm_call=_llm_raising)
    assert r is None


async def _make_task():
    async with SessionLocal() as session:
        task = Task(idempotency_key=f"s-{uuid.uuid4().hex[:8]}", query="q",
                    content_type="x", mode="general")
        session.add(task)
        await session.commit()
        await session.refresh(task)
        tid = task.id
        for i in range(1, 7):
            session.add(PageCopy(task_id=tid, page_index=i, body=f"第{i}页文案", claim_ids=[]))
        await session.commit()
    return tid


@pytest.mark.asyncio
async def test_asset_gen_injects_subjects_and_persists():
    import json
    tid = await _make_task()
    fake_llm = AsyncMock(return_value={"text": json.dumps(SUBJECTS, ensure_ascii=False),
                                       "model_version": "mock", "cost_cny": 0})
    with patch("src.pipeline.nodes.call_with_failover", fake_llm), \
         patch("src.gateway.image_gen.generate_image", new=AsyncMock(return_value=FAKE_IMAGE)) as gen:
        await node_asset_gen({"task_id": tid})
    # 6 页提示词各自注入对应页的动态主体句，通用锚定条款被替换
    # （去重/质检会对同 hash 的 mock 图重生成，调用顺序不按页码，故按内容断言）
    prompts = [call.args[0] for call in gen.await_args_list]
    for i in range(1, 7):
        assert any(f"本页画面主体必须是：主体{i}，占据画面视觉中心" in p
                   for p in prompts)
    assert all("画面就必须出现牛奶饮品杯" not in p for p in prompts)
    # 提取结果落 tasks.page_subjects 便于排查
    async with SessionLocal() as session:
        t = (await session.execute(select(Task).where(Task.id == tid))).scalar_one()
    assert t.page_subjects == SUBJECTS


@pytest.mark.asyncio
async def test_asset_gen_extract_failure_keeps_generic_anchor():
    tid = await _make_task()
    with patch("src.pipeline.nodes.call_with_failover",
               new=AsyncMock(side_effect=RuntimeError("LLM down"))), \
         patch("src.gateway.image_gen.generate_image", new=AsyncMock(return_value=FAKE_IMAGE)) as gen:
        await node_asset_gen({"task_id": tid})
    # 提取失败不阻塞出图：提示词沿用通用锚定条款，page_subjects 保持 NULL
    prompts = [call.args[0] for call in gen.await_args_list]
    assert prompts
    for prompt in prompts:
        assert "本页画面主体必须是：" not in prompt
        assert "画面就必须出现牛奶饮品杯" in prompt
    async with SessionLocal() as session:
        t = (await session.execute(select(Task).where(Task.id == tid))).scalar_one()
    assert t.page_subjects is None
