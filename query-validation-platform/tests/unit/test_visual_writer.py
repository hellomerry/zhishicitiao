"""场景化视觉扩写器单测（2026-09-02，迁移 020）。

LLM 调用全部注入 fake；DB 笔记池走测试库（conftest 隔离）。
"""
import pytest

from src.services import visual_writer
from src.services.visual_writer import (add_visual_note, write_page_visuals,
                                        _parse_visual)
from src.gateway.prompt_versions import get_image_prompt

GOOD_JSON = ('{"style_en": "Warm cream base, sage green accents, soft morning '
             'light, clean flat illustration.", "pages": [' +
             ",".join(f'"visual direction page {i}"' for i in range(1, 7)) +
             ']}')


class TestParseVisual:
    def test_valid(self):
        v = _parse_visual(GOOD_JSON)
        assert v and v["style_en"].startswith("Warm cream")
        assert len(v["pages"]) == 6

    def test_with_markdown_fence(self):
        assert _parse_visual("```json\n" + GOOD_JSON + "\n```") is not None

    def test_bad_json(self):
        assert _parse_visual("not json") is None

    def test_missing_pages(self):
        assert _parse_visual('{"style_en": "x", "pages": ["a"]}') is None

    def test_empty_page_rejected(self):
        assert _parse_visual('{"style_en": "x", "pages": ["a","b","c","d","e",""]}') is None


@pytest.mark.asyncio
async def test_write_page_visuals_injects_notes():
    """笔记池内容注入扩写提示词（驳回反馈越用越贴合的关键路径）。"""
    await add_visual_note("背景太白像没设计过", source="test")
    captured = []

    async def fake_llm(prompt):
        captured.append(prompt)
        return {"text": GOOD_JSON}

    v = await write_page_visuals("治愈暖彩", "柔和暖色调", ["文案一", "文案二"],
                                 llm_call=fake_llm)
    assert v is not None
    assert "背景太白像没设计过" in captured[0]  # 笔记注入
    assert "治愈暖彩" in captured[0]            # 风格库注入
    assert "文案一" in captured[0]              # 分页文案注入


@pytest.mark.asyncio
async def test_write_page_visuals_failure_returns_none():
    async def bad_llm(prompt):
        return {"text": "LLM 闲聊而不是 JSON"}

    assert await write_page_visuals("s", "d", ["x"], llm_call=bad_llm) is None


class TestEnglishNotextPrompt:
    VIS = "A corgi eating tomato chunks in a warm kitchen, soft side light."
    STYLE_EN = "Warm cream base, sage green accents, flat illustration."

    def test_en_skeleton_used_when_visual(self):
        p = get_image_prompt("general", "正文", 2, no_text=True,
                             visual=self.VIS, style_en=self.STYLE_EN)
        assert "TEXT-FREE" in p
        assert self.VIS in p and self.STYLE_EN in p
        assert "top two-fifths" in p          # 槽位 2 顶部留白区（英文）
        assert "本页" not in p                # 不混入中文骨架

    def test_en_zone_follows_force_slot(self):
        p = get_image_prompt("general", "正文", 6, no_text=True, force_slot=2,
                             visual=self.VIS, style_en=self.STYLE_EN)
        assert "top two-fifths" in p          # force_slot 生效（classic_pills）

    def test_compare_and_refs_rules(self):
        p = get_image_prompt("compare", "正文", 1, no_text=True,
                             visual=self.VIS, style_en=self.STYLE_EN)
        assert "BOTH subjects side by side" in p   # 对比硬性要求
        assert "reference photo" in p              # 实景图融入规则

    def test_fallback_chinese_when_no_visual(self):
        p = get_image_prompt("general", "正文", 2, no_text=True)
        assert "本页" in p and "TEXT-FREE" not in p
