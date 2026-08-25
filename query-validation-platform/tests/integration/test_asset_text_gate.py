"""asset_gen 文字质检门（OCR 对撞 + 有限重生成）的集成测试。

背景：2026-08-25 用户反馈「文字扭曲（伪汉字/乱码）概率非常大」。
asset_gen 在非 mock 模式下，每张图产出后立即 OCR 并与分页文案比对，
低于阈值自动换构图重生成（最多 asset_text_max_attempts 次），
仍不达标在 model_version 打 text_garble 标记交人工审核。
"""
import io
import re
import uuid

import pytest
from unittest.mock import AsyncMock, patch
from sqlalchemy import select

from src.config import settings
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.models.drafts import PageCopy
from src.models.assets import Asset
from src.pipeline.nodes import node_asset_gen


def _png_bytes(n: int) -> bytes:
    """每次返回不同内容的 PNG（否则内容级去重会误判重复）。"""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (750, 1000), (n % 256, 100, 200)).save(buf, "PNG")
    return buf.getvalue()


async def _make_task() -> str:
    async with SessionLocal() as session:
        task = Task(idempotency_key=f"g-{uuid.uuid4().hex[:8]}", query="q",
                    content_type="x", mode="general")
        session.add(task)
        await session.commit()
        await session.refresh(task)
        tid = str(task.id)
        for i in range(1, 7):
            session.add(PageCopy(task_id=tid, page_index=i,
                                 body=f"第{i}页核心要点", claim_ids=[]))
        await session.commit()
    return tid


def _gen_side(calls):
    def side(prompt, reference_image_urls=None):
        calls["gen"] += 1
        return {"hash": f"h{calls['gen']}",
                "image_url": f"https://example.com/{calls['gen']}.png",
                "model_version": "gpt-image-2"}
    return side


def _patch_common(calls, ocr_side):
    """mock_image_gen=False + 全部外部依赖打桩（不落盘、不联网）。"""

    async def fetch_side(url):
        calls["fetch"] += 1
        return _png_bytes(calls["fetch"]), "image/png"

    return (
        patch.object(settings, "mock_image_gen", False),
        patch.object(settings, "image_gen_delay_seconds", 0),
        patch("src.gateway.image_gen.generate_image",
              new=AsyncMock(side_effect=_gen_side(calls))),
        patch("src.gateway.ocr.fetch_image_bytes",
              new=AsyncMock(side_effect=fetch_side)),
        patch("src.pipeline.nodes._persist_image",
              side_effect=lambda tid, idx, tag, data, ct: f"/static/generated/f{idx}.png"),
        patch("src.gateway.ocr.ocr_image",
              new=AsyncMock(side_effect=ocr_side)),
    )


def _good_ocr(url):
    idx = int(re.search(r"/f(\d+)\.png", url).group(1))
    return {"raw_text": f"第{idx}页核心要点", "cost_cny": 0.001, "model": "qwen-vl-ocr"}


async def _db_assets(tid):
    async with SessionLocal() as session:
        r = await session.execute(
            select(Asset).where(Asset.task_id == tid,
                                Asset.source_type == "ai_generated"))
        return r.scalars().all()


@pytest.mark.asyncio
async def test_text_gate_clean_images_no_regen():
    """OCR 全部达标：6 张图各生成一次，无 text_garble 标记，成本含 OCR 费。"""
    tid = await _make_task()
    calls = {"gen": 0, "fetch": 0}
    patches = _patch_common(calls, _good_ocr)
    with patches[0], patches[1], patches[2] as gen, patches[3], patches[4], patches[5] as ocr:
        out = await node_asset_gen({"task_id": tid})
    assert gen.await_count == 6
    assert ocr.await_count == 6
    assert abs(out["cost_cny"] - (6 * 0.2 + 6 * 0.001)) < 1e-9
    assets = await _db_assets(tid)
    assert len(assets) == 6
    assert all("text_garble" not in a.model_version for a in assets)


@pytest.mark.asyncio
async def test_text_gate_regen_once_on_garble():
    """第 1 页首次 OCR 判扭曲 → 自动重生成一次后达标，无标记。"""
    tid = await _make_task()
    calls = {"gen": 0, "fetch": 0}
    ocr_calls = {"n": 0}

    def ocr_side(url):
        ocr_calls["n"] += 1
        if ocr_calls["n"] == 1:  # 仅第一次识别为乱码
            return {"raw_text": "◆■□乱码xyz", "cost_cny": 0.001, "model": "qwen-vl-ocr"}
        return _good_ocr(url)

    patches = _patch_common(calls, ocr_side)
    with patches[0], patches[1], patches[2] as gen, patches[3], patches[4], patches[5]:
        await node_asset_gen({"task_id": tid})
    assert gen.await_count == 7  # 6 张 + 第 1 页重生成 1 次
    assets = await _db_assets(tid)
    assert all("text_garble" not in a.model_version for a in assets)


@pytest.mark.asyncio
async def test_text_gate_persistent_garble_marked():
    """持续扭曲（重生成 asset_text_max_attempts 次仍不达标）→ 打 text_garble 标记。"""
    tid = await _make_task()
    calls = {"gen": 0, "fetch": 0}

    def bad_ocr(url):
        return {"raw_text": "◆■□乱码xyz", "cost_cny": 0.001, "model": "qwen-vl-ocr"}

    patches = _patch_common(calls, bad_ocr)
    with patches[0], patches[1], patches[2] as gen, patches[3], patches[4], patches[5]:
        await node_asset_gen({"task_id": tid})
    # 每页：1 次首生成 + max_attempts 次重生成
    assert gen.await_count == 6 * (1 + settings.asset_text_max_attempts)
    assets = await _db_assets(tid)
    assert len(assets) == 6
    assert all("text_garble" in a.model_version for a in assets)


@pytest.mark.asyncio
async def test_text_gate_ocr_outage_not_blocking():
    """OCR 服务异常时不阻塞出图、不重生成（视为未知）。"""
    tid = await _make_task()
    calls = {"gen": 0, "fetch": 0}

    def boom(url):
        raise RuntimeError("ocr service down")

    patches = _patch_common(calls, boom)
    with patches[0], patches[1], patches[2] as gen, patches[3], patches[4], patches[5]:
        await node_asset_gen({"task_id": tid})
    assert gen.await_count == 6
    assets = await _db_assets(tid)
    assert all("text_garble" not in a.model_version for a in assets)
