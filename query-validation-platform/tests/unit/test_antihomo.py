"""反同质化（2026-09-02）测试：#1 页位偏移 + #2 风格变体轴。

#1：分页布局/文字形式/合成槽位的轮换起点按 task_id 偏移——每个任务的第 N 页
   不再永远套第 N 种布局；无字版偏移与 text_composite 落版槽位必须同值对齐。
#2：风格 = 签名层（描述词）+ 变体层（variants 池按任务采样追加），同一用户
   同一风格每篇不重样；DB 条目 variants 优先于内置变体池。
"""
import io

import pytest
from sqlalchemy import text

from src.db.session import SessionLocal
from src.gateway import prompt_versions as pv
from src.gateway.prompt_versions import get_image_prompt
from src.pipeline.nodes import _layout_offset_for
from src.services import text_composite as tc
from src.services.style_pick import variant_for, _BUILTIN_VARIANTS


# ---------- #1 页位偏移 ----------

def test_layout_offset_rotates_page_layouts():
    p0 = get_image_prompt("general", "文案", 1)
    p1 = get_image_prompt("general", "文案", 1, layout_offset=1)
    assert pv._PAGE_LAYOUTS[0] in p0 and pv._TEXT_PRESENTATIONS[0] in p0
    assert pv._PAGE_LAYOUTS[1] in p1 and pv._TEXT_PRESENTATIONS[1] in p1


def test_layout_offset_six_pages_still_distinct():
    """任意偏移下 6 页仍各用 6 种不同布局/文字形式（不破坏原轮换目的）。"""
    for off in range(6):
        layouts = {pv._PAGE_LAYOUTS[(i - 1 + off) % 6] for i in range(1, 7)}
        assert len(layouts) == 6
        prompts = [get_image_prompt("general", "文案", i, layout_offset=off)
                   for i in range(1, 7)]
        assert len(set(prompts)) == 6


def test_layout_offset_notext_mode():
    p = get_image_prompt("general", "文案", 1, no_text=True, layout_offset=2)
    assert pv._PAGE_LAYOUTS_NOTEXT[2] in p


def test_layout_offset_for_task_deterministic():
    o1 = _layout_offset_for("task-abc")
    assert 0 <= o1 < 6 and o1 == _layout_offset_for("task-abc")


def _png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (750, 1000), (240, 230, 210)).save(buf, "PNG")
    return buf.getvalue()


def test_composite_page_offset_aligns_slot(monkeypatch):
    """合成落版槽位随 offset 偏移（与提示词留白区用同一偏移值保持对齐）。"""
    captured = {}
    monkeypatch.setattr(tc, "_font_paths", lambda: ("reg", "bold"))
    monkeypatch.setattr(tc, "_draw_text_block",
                        lambda img, t, b, slot: captured.setdefault("slot", slot))
    out = tc.composite_page(_png(), "标题：正文内容", 1, offset=2)
    assert out is not None
    assert captured["slot"] == 3  # ((1-1+2) % 6) + 1
    # 同一页同一偏移：提示词留白区索引与合成槽位索引一致（对齐契约）
    off = 2
    layout = pv._PAGE_LAYOUTS_NOTEXT[(1 - 1 + off) % 6]
    assert "底部" in layout or "上部" in layout or "中部" in layout
    assert ((1 - 1 + off) % 6) + 1 == captured["slot"]


# ---------- #2 风格变体轴 ----------

@pytest.mark.asyncio
async def test_variant_for_builtin_deterministic():
    frags = _BUILTIN_VARIANTS["自然写实暖调"]
    v0 = await variant_for("自然写实暖调", None, seed=0)
    assert v0 == "本篇风格变体：" + frags[0] + "。"
    assert v0 == await variant_for("自然写实暖调", None, seed=0)  # 同 seed 稳定
    v1 = await variant_for("自然写实暖调", None, seed=1)
    assert v1 == "本篇风格变体：" + frags[1 % len(frags)] + "。"


@pytest.mark.asyncio
async def test_variant_for_unknown_style_none():
    assert await variant_for("不存在的风格", None, seed=0) is None
    assert await variant_for("", None, seed=0) is None


@pytest.mark.asyncio
async def test_variant_for_db_overrides_builtin():
    """个人库条目设了 variants → 优先用 DB 池（用户可自定义变体轴）。"""
    import uuid
    from src.models.styles import StyleKeyword
    name = f"测试变体风{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as s:
        uid = (await s.execute(text(
            "INSERT INTO users (name, role) VALUES (:n, 'A') RETURNING id"),
            {"n": f"u-{uuid.uuid4().hex[:8]}"})).scalar()
        s.add(StyleKeyword(owner_id=uid, style_name=name, keywords="",
                           description="d", variants="甲变体；乙变体\n丙变体"))
        await s.commit()
    assert await variant_for(name, uid, seed=0) == "本篇风格变体：甲变体。"
    assert await variant_for(name, uid, seed=1) == "本篇风格变体：乙变体。"
    assert await variant_for(name, uid, seed=2) == "本篇风格变体：丙变体。"
    assert await variant_for(name, uid, seed=3) == "本篇风格变体：甲变体。"
