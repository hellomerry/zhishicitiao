"""asset_gen VL 视觉二审 + 并行出图的集成测试。

背景：2026-09-01 借鉴 8003（ai_review / IMAGE_GEN_PARALLEL）：
- VL 二审：OCR 文字关卡之后，qwen-vl 再查文字过载/实景嵌入不协调，
  不达标带 VL 建议自动重生成（最多 vl_review_max_rounds 轮），仍败打
  |vl_flag 标记交人工审核；VL 服务异常默认通过不阻塞。
- 并行出图：image_gen_parallel>1 时 6 页 Semaphore 限流并发生成。
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
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (750, 1000), (n % 256, 100, 200)).save(buf, "PNG")
    return buf.getvalue()


async def _make_task(with_ref: bool = True) -> str:
    async with SessionLocal() as session:
        task = Task(idempotency_key=f"v-{uuid.uuid4().hex[:8]}", query="q",
                    content_type="x", mode="general")
        session.add(task)
        await session.commit()
        await session.refresh(task)
        tid = str(task.id)
        for i in range(1, 7):
            session.add(PageCopy(task_id=tid, page_index=i,
                                 body=f"第{i}页核心要点", claim_ids=[]))
        if with_ref:
            # 有 official 实图 → 全页触发 VL 二审（无实图且文字关卡干净的页会跳过）
            session.add(Asset(task_id=tid, page_index=0, source_type="official",
                              copyright_status="clear", hash="refhash",
                              image_url="/static/generated/ref.png",
                              origin_url="https://example.com/ref.png",
                              model_version="search", is_illustration=False))
        await session.commit()
    return tid


def _gen_side(calls):
    def side(prompt, reference_image_urls=None, provider=None):
        calls["gen"] += 1
        return {"hash": f"h{calls['gen']}",
                "image_url": f"https://example.com/{calls['gen']}.png",
                "model_version": "gpt-image-2"}
    return side


def _patch_common(calls, vl_side):
    async def fetch_side(url):
        calls["fetch"] += 1
        return _png_bytes(calls["fetch"]), "image/png"

    async def ocr_side(url):
        idx = int(re.search(r"/f(\d+)\.png", url).group(1))
        return {"raw_text": f"第{idx}页核心要点", "cost_cny": 0.001,
                "model": "qwen-vl-ocr"}

    return (
        patch.object(settings, "mock_image_gen", False),
        patch.object(settings, "image_gen_delay_seconds", 0),
        patch.object(settings, "text_composite_enabled", False),
        patch("src.gateway.image_gen.generate_image",
              new=AsyncMock(side_effect=_gen_side(calls))),
        patch("src.gateway.ocr.fetch_image_bytes",
              new=AsyncMock(side_effect=fetch_side)),
        patch("src.pipeline.nodes._persist_image",
              side_effect=lambda tid, idx, tag, data, ct: f"/static/generated/f{idx}.png"),
        patch("src.gateway.ocr.ocr_image", new=AsyncMock(side_effect=ocr_side)),
        patch("src.services.vl_review.vl_review_image",
              new=AsyncMock(side_effect=vl_side)),
    )


async def _db_assets(tid):
    async with SessionLocal() as session:
        r = await session.execute(
            select(Asset).where(Asset.task_id == tid,
                                Asset.source_type == "ai_generated"))
        return r.scalars().all()


_GOOD_VL = {"pass": True, "issues": [], "suggest": "", "flags": {}, "cost_cny": 0.002}


@pytest.mark.asyncio
async def test_vl_gate_clean_images_pass():
    """VL 全部达标：6 张图各生成一次，无 vl_flag 标记，成本含 VL 费。"""
    tid = await _make_task()
    calls = {"gen": 0, "fetch": 0}
    patches = _patch_common(calls, lambda *a, **k: dict(_GOOD_VL))
    with patches[0], patches[1], patches[2], patches[3] as gen, patches[4], \
            patches[5], patches[6], patches[7] as vl:
        out = await node_asset_gen({"task_id": tid})
    assert gen.await_count == 6
    assert vl.await_count == 6  # 有实图 → 全页 VL 二审
    assert abs(out["cost_cny"] - (6 * 0.2 + 6 * 0.001 + 6 * 0.002)) < 1e-9
    assets = await _db_assets(tid)
    assert len(assets) == 6
    assert all("vl_flag" not in a.model_version for a in assets)


@pytest.mark.asyncio
async def test_vl_gate_regen_once_then_pass():
    """第 1 页首轮 VL 判实景不协调 → 带建议重生成一次后达标，无标记。"""
    tid = await _make_task()
    calls = {"gen": 0, "fetch": 0}
    vl_calls = {"n": 0}

    def vl_side(*a, **k):
        vl_calls["n"] += 1
        if vl_calls["n"] == 1:
            return {"pass": False, "issues": ["实景图遮挡主体"],
                    "suggest": "缩小实景图并移到角落", "flags": {"ref_ok": False},
                    "cost_cny": 0.002}
        return dict(_GOOD_VL)

    patches = _patch_common(calls, vl_side)
    with patches[0], patches[1], patches[2], patches[3] as gen, patches[4], \
            patches[5], patches[6], patches[7]:
        await node_asset_gen({"task_id": tid})
    assert gen.await_count == 7  # 6 张 + 第 1 页重生成 1 次
    assets = await _db_assets(tid)
    assert all("vl_flag" not in a.model_version for a in assets)


@pytest.mark.asyncio
async def test_vl_gate_persistent_fail_marked():
    """VL 持续不达标（vl_review_max_rounds 轮后）→ 打 vl_flag 标记交人工审核。"""
    tid = await _make_task()
    calls = {"gen": 0, "fetch": 0}

    def bad_vl(*a, **k):
        return {"pass": False, "issues": ["文字过载"], "suggest": "精简文字",
                "flags": {"text_amount_ok": False}, "cost_cny": 0.002}

    patches = _patch_common(calls, bad_vl)
    with patches[0], patches[1], patches[2], patches[3] as gen, patches[4], \
            patches[5], patches[6], patches[7]:
        await node_asset_gen({"task_id": tid})
    # 每页：1 次首生成 + (max_rounds - 1) 次重生成
    assert gen.await_count == 6 * settings.vl_review_max_rounds
    assets = await _db_assets(tid)
    assert len(assets) == 6
    assert all("vl_flag:文字过载" in a.model_version for a in assets)


@pytest.mark.asyncio
async def test_vl_gate_outage_not_blocking():
    """VL 服务异常：vl_review_image 内部 fail-open，不阻塞、不重生成。"""
    tid = await _make_task()
    calls = {"gen": 0, "fetch": 0}

    def outage(*a, **k):
        # vl_review_image 自身承诺异常默认通过；这里模拟其 fail-open 返回
        return {"pass": True, "issues": [], "suggest": "", "flags": {}, "cost_cny": 0.0}

    patches = _patch_common(calls, outage)
    with patches[0], patches[1], patches[2], patches[3] as gen, patches[4], \
            patches[5], patches[6], patches[7]:
        await node_asset_gen({"task_id": tid})
    assert gen.await_count == 6
    assets = await _db_assets(tid)
    assert all("vl_flag" not in a.model_version for a in assets)


@pytest.mark.asyncio
async def test_vl_gate_disabled_skips():
    """vl_review_enabled=false：不调用 VL、不产生 VL 成本。"""
    tid = await _make_task()
    calls = {"gen": 0, "fetch": 0}
    patches = _patch_common(calls, lambda *a, **k: dict(_GOOD_VL))
    with patch.object(settings, "vl_review_enabled", False), \
            patches[0], patches[1], patches[2], patches[3] as gen, patches[4], \
            patches[5], patches[6], patches[7] as vl:
        out = await node_asset_gen({"task_id": tid})
    assert vl.await_count == 0
    assert abs(out["cost_cny"] - (6 * 0.2 + 6 * 0.001)) < 1e-9


@pytest.mark.asyncio
async def test_parallel_gen_produces_six_assets():
    """image_gen_parallel=4：并发路径同样产出 6 张图且顺序正确。"""
    tid = await _make_task()
    calls = {"gen": 0, "fetch": 0}
    patches = _patch_common(calls, lambda *a, **k: dict(_GOOD_VL))
    with patch.object(settings, "image_gen_parallel", 4), \
            patches[0], patches[1], patches[2], patches[3] as gen, patches[4], \
            patches[5], patches[6], patches[7]:
        out = await node_asset_gen({"task_id": tid})
    assert gen.await_count == 6
    assert out["asset_count"] == 6
    assets = await _db_assets(tid)
    assert sorted(a.page_index for a in assets) == [1, 2, 3, 4, 5, 6]
