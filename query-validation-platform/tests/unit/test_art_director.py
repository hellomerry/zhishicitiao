"""视觉策划（src/services/art_director.py）+ get_image_prompt 方案注入：

- normalize_plan：恰好 6 页/必填字段/长度截断/title_zone 合法化
- generate_plan：mock LLM 成功解析；垃圾输出/异常 → None（不阻塞出图）
- finalize_plan：剥离成本、无字模式 title_zone 按代码槽位覆盖（合成落版对齐）
- get_image_prompt(plan_page=...)：有字版方案接管构图与文字载体（不再追加固定
  轮换）；无字版保留代码槽位留白区、只追加画面创意
"""
import json

import pytest

from src.services.art_director import (
    build_plan_prompt, finalize_plan, generate_plan, normalize_plan)
from src.gateway.prompt_versions import (
    _PAGE_LAYOUTS, _PAGE_LAYOUTS_NOTEXT, _TEXT_PRESENTATIONS, get_image_prompt)


def _pages(over=None):
    over = over or {}
    pages = []
    for i in range(1, 7):
        p = {"page": i, "composition": f"构图{i}：主体居中偏上，文字区置底",
             "text_form": f"形式{i}", "palette": f"暖调{i}",
             "elements": f"元素{i}", "focus": f"焦点{i}", "title_zone": "top"}
        p.update(over.get(i, {}))
        pages.append(p)
    return {"pages": pages}


def test_normalize_ok_reindex_and_zone_default():
    plan = normalize_plan(_pages())
    assert [p["page"] for p in plan["pages"]] == [1, 2, 3, 4, 5, 6]
    # 非法 zone 回退合法值
    bad = _pages({3: {"title_zone": "weird"}})
    plan = normalize_plan(bad)
    assert plan["pages"][2]["title_zone"] in ("top", "middle", "bottom")


def test_normalize_rejects_bad_shape():
    assert normalize_plan({"pages": _pages()["pages"][:5]}) is None      # 只有 5 页
    assert normalize_plan({"pages": "x"}) is None
    assert normalize_plan(None) is None
    no_comp = _pages({2: {"composition": ""}})
    assert normalize_plan(no_comp) is None                                # 构图必填
    no_focus = _pages({4: {"focus": ""}})
    assert normalize_plan(no_focus) is None                               # 焦点必填


def test_normalize_truncates_long_fields():
    long = _pages({1: {"composition": "长" * 300, "palette": "色" * 100}})
    plan = normalize_plan(long)
    assert len(plan["pages"][0]["composition"]) == 100
    assert len(plan["pages"][0]["palette"]) == 25


def _llm_returning(text):
    async def _call(prompt):
        return {"text": text, "model_version": "mock", "cost_cny": 0.01}
    return _call


@pytest.mark.asyncio
async def test_generate_plan_success():
    r = await generate_plan("选题", "general", [f"文案{i}" for i in range(1, 7)],
                            llm_call=_llm_returning(
                                json.dumps(_pages(), ensure_ascii=False)))
    assert r and len(r["pages"]) == 6
    assert r["model"] == "mock" and r["cost_cny"] == 0.01


@pytest.mark.asyncio
async def test_generate_plan_garbage_and_error_return_none():
    assert await generate_plan("q", "general", [], llm_call=_llm_returning("不是JSON")) is None

    async def _boom(prompt):
        raise RuntimeError("LLM down")
    assert await generate_plan("q", "general", [], llm_call=_boom) is None


def test_build_prompt_injects_context_and_feedback():
    p = build_plan_prompt("冰牛奶做法", "general", ["a"] * 6,
                          style_desc="治愈暖调", has_refs=True,
                          feedback="封面太挤")
    assert "冰牛奶做法" in p and "治愈暖调" in p and "封面太挤" in p
    assert "实景参考图" in p


def test_finalize_plan_strips_cost_and_overrides_zone_in_notext():
    plan = _pages()
    plan["cost_cny"] = 0.5
    out = finalize_plan(plan, style="治愈暖彩", no_text=True, layout_offset=0)
    assert "cost_cny" not in out and out["style"] == "治愈暖彩"
    # offset=0 时槽位 1..6 → _ZONE_BY_PAGE: bottom/top/bottom/top/top/center
    zones = [p["title_zone"] for p in out["pages"]]
    assert zones == ["bottom", "top", "bottom", "top", "top", "center"]
    # 有字版保留策划的 title_zone
    out2 = finalize_plan(_pages(), style=None, no_text=False)
    assert all(p["title_zone"] == "top" for p in out2["pages"])


def test_image_prompt_with_plan_replaces_rotation():
    plan_page = _pages()["pages"][0]
    p = get_image_prompt("general", "本页文案", 1, no_text=False,
                         plan_page=plan_page)
    assert "本页创意方案" in p and "构图1" in p and "形式1" in p
    # 固定轮换不再追加
    assert _PAGE_LAYOUTS[0] not in p and _TEXT_PRESENTATIONS[0] not in p
    # 无方案时保持旧轮换
    p2 = get_image_prompt("general", "本页文案", 1, no_text=False)
    assert _PAGE_LAYOUTS[0] in p2 and _TEXT_PRESENTATIONS[0] in p2


def test_image_prompt_notext_keeps_zone_and_adds_creative():
    plan_page = _pages()["pages"][1]
    p = get_image_prompt("general", "本页文案", 2, no_text=True,
                         plan_page=plan_page)
    # 代码槽位留白区指令保留（合成落版对齐）
    assert _PAGE_LAYOUTS_NOTEXT[1] in p
    # 方案只以画面创意形式追加，且强调留白区保护
    assert "本页画面创意" in p and "构图2" in p and "留白区" in p
    # 无方案的无字版不追加创意段
    p2 = get_image_prompt("general", "本页文案", 2, no_text=True)
    assert "本页画面创意" not in p2
