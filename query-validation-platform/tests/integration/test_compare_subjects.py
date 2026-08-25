"""compare 模式主体拆分搜图 + asset_gen 分主体参考图路由的测试。

背景：2026-08-25 用户反馈②③④——实景图使用错误、对比类每页必须双主体同框、
对比要覆盖多角度（整体/正面/侧面/细节）。
entity_bind 对 compare 先用 LLM（失败退回启发式）把 query 拆成主体 A/B，
各自独立搜整体图 + 细节图并打 A:/B: 标签；asset_gen 按页轮换喂 A、B 各 2 张参考图。
"""
import uuid

import pytest
from unittest.mock import AsyncMock, patch
from sqlalchemy import select

from src.db.session import SessionLocal
from src.models.tasks import Task
from src.models.assets import Asset
from src.models.drafts import PageCopy
from src.pipeline.nodes import node_entity_bind, node_asset_gen


def _img(url):
    return {"title": "t", "image_url": url, "source": "s", "engine": "bing"}


async def _fake_fetch(url):
    return b"\x89PNG fake", "image/png"


async def _make_task(mode, query="小米17 Pro 和 荣耀600 Pro 怎么选"):
    async with SessionLocal() as session:
        task = Task(idempotency_key=f"c-{uuid.uuid4().hex[:8]}", query=query,
                    content_type="x", mode=mode)
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task.id


@pytest.mark.asyncio
async def test_compare_splits_subjects_and_searches_separately():
    """compare：拆出 A/B 后各自搜整体图 3 张 + 细节图 2 张，Asset 打主体标签。"""
    tid = await _make_task("compare")
    calls = []

    async def fake_search(q, count=6):
        calls.append((q, count))
        return [_img(f"https://example.com/s{len(calls)}-{i}.png") for i in range(count)]

    with patch("src.pipeline.nodes._split_compare_subjects",
               new=AsyncMock(return_value=("小米17 Pro", "荣耀600 Pro"))), \
         patch("src.gateway.image_search.search_image", side_effect=fake_search), \
         patch("src.gateway.ocr.fetch_image_bytes", side_effect=_fake_fetch), \
         patch("src.pipeline.nodes._persist_image",
               side_effect=lambda tid_, i, tag, data, ct: f"/static/generated/ref{i}.png"):
        out = await node_entity_bind({"task_id": tid})

    assert out["searched_images"] == 10
    assert out["subjects"] == ["小米17 Pro", "荣耀600 Pro"]
    assert calls == [("小米17 Pro", 3), ("小米17 Pro 细节 侧面", 2),
                     ("荣耀600 Pro", 3), ("荣耀600 Pro 细节 侧面", 2)]
    async with SessionLocal() as session:
        assets = (await session.execute(
            select(Asset).where(Asset.task_id == tid))).scalars().all()
    a_tags = [a.subject for a in assets if a.subject.startswith("A:")]
    b_tags = [a.subject for a in assets if a.subject.startswith("B:")]
    assert len(a_tags) == 5 and len(b_tags) == 5
    assert any("细节" in t for t in a_tags)
    assert any("细节" in t for t in b_tags)


@pytest.mark.asyncio
async def test_compare_fallback_whole_query_when_split_fails():
    """拆分失败：退回整词搜索 6 张（旧行为），subject 为整条 query。"""
    tid = await _make_task("compare", query="某种无法拆分的查询")

    async def fake_search(q, count=6):
        return [_img(f"https://example.com/x-{i}.png") for i in range(count)]

    with patch("src.pipeline.nodes._split_compare_subjects",
               new=AsyncMock(return_value=None)), \
         patch("src.gateway.image_search.search_image",
               side_effect=fake_search) as search, \
         patch("src.gateway.ocr.fetch_image_bytes", side_effect=_fake_fetch), \
         patch("src.pipeline.nodes._persist_image",
               side_effect=lambda tid_, i, tag, data, ct: f"/static/generated/ref{i}.png"):
        out = await node_entity_bind({"task_id": tid})

    assert out["searched_images"] == 6
    assert out["subjects"] == []
    assert search.await_count == 1
    async with SessionLocal() as session:
        assets = (await session.execute(
            select(Asset).where(Asset.task_id == tid))).scalars().all()
    assert all(a.subject == "某种无法拆分的查询" for a in assets)


@pytest.mark.asyncio
async def test_asset_gen_compare_routes_refs_per_page():
    """asset_gen compare：每页参考图 = A 池 2 张 + B 池 2 张，且按页轮换不同图。"""
    tid = await _make_task("compare")
    async with SessionLocal() as session:
        for i in range(1, 7):
            session.add(PageCopy(task_id=tid, page_index=i, body=f"第{i}页",
                                 claim_ids=[]))
        for j in range(5):
            session.add(Asset(task_id=tid, page_index=0, subject="A:小米17 Pro",
                              source_type="official", copyright_status="unknown",
                              hash=f"a{j}", image_url=f"https://example.com/A{j}.png",
                              model_version="s", is_illustration=False))
            session.add(Asset(task_id=tid, page_index=0, subject="B:荣耀600 Pro",
                              source_type="official", copyright_status="unknown",
                              hash=f"b{j}", image_url=f"https://example.com/B{j}.png",
                              model_version="s", is_illustration=False))
        await session.commit()

    fake = {"hash": "x", "image_url": "https://example.com/out.png",
            "model_version": "gpt-image-2"}
    with patch("src.config.settings.mock_image_gen", True), \
         patch("src.gateway.image_gen.generate_image",
               new=AsyncMock(return_value=fake)) as gen:
        await node_asset_gen({"task_id": tid})

    per_page = [c.kwargs.get("reference_image_urls") for c in gen.await_args_list]
    assert len(per_page) == 6
    for refs in per_page:
        a = [u for u in refs if "/A" in u]
        b = [u for u in refs if "/B" in u]
        assert len(a) == 2 and len(b) == 2, f"每页应各含 A/B 两张参考图: {refs}"
    # 按页轮换：第 1 页与第 2 页拿到的参考图组合应不同
    assert per_page[0] != per_page[1]


@pytest.mark.asyncio
async def test_asset_gen_compare_legacy_untagged_refs_unchanged():
    """无 A/B 标签的存量参考图：退回全量喂图（兼容旧数据）。"""
    tid = await _make_task("compare")
    async with SessionLocal() as session:
        for i in range(1, 7):
            session.add(PageCopy(task_id=tid, page_index=i, body=f"第{i}页",
                                 claim_ids=[]))
        for j in range(3):
            session.add(Asset(task_id=tid, page_index=0, subject="整条query",
                              source_type="official", copyright_status="unknown",
                              hash=f"c{j}", image_url=f"https://example.com/C{j}.png",
                              model_version="s", is_illustration=False))
        await session.commit()

    fake = {"hash": "x", "image_url": "https://example.com/out.png",
            "model_version": "gpt-image-2"}
    with patch("src.config.settings.mock_image_gen", True), \
         patch("src.gateway.image_gen.generate_image",
               new=AsyncMock(return_value=fake)) as gen:
        await node_asset_gen({"task_id": tid})

    for c in gen.await_args_list:
        refs = c.kwargs.get("reference_image_urls")
        assert refs == ["https://example.com/C0.png",
                        "https://example.com/C1.png",
                        "https://example.com/C2.png"]
