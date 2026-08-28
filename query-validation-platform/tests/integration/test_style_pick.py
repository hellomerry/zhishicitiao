"""风格自适应选择（src/services/style_pick.py）：

- LLM mock：从用户风格库中选出最贴合的风格
- LLM 失败/结果不在库中 → 关键词命中评分兜底
- 用户库为空 → 回退内置 8 风格
- style_desc_for 反查：用户库优先，内置兜底，未知返回 None
"""
import pytest

from src.db.session import SessionLocal
from src.models.styles import StyleKeyword
from src.services.style_pick import (IMAGE_STYLE_LIBRARY, pick_image_style,
                                     style_desc_for)

_BUILTIN_NAMES = {n for n, _, _ in IMAGE_STYLE_LIBRARY}


async def _add_style(name, keywords="", description="", enabled=True):
    async with SessionLocal() as session:
        session.add(StyleKeyword(style_name=name, keywords=keywords,
                                 description=description, enabled=enabled))
        await session.commit()


def _llm_returning(text):
    async def _call(prompt):
        return {"text": text, "model_version": "mock", "cost_cny": 0}
    return _call


def _llm_raising(prompt):
    raise RuntimeError("LLM down")


@pytest.mark.asyncio
async def test_llm_picks_from_user_library():
    await _add_style("科技蓝调", "手机,数码", "深蓝科技光感")
    await _add_style("暖木家居", "家具,装修", "暖木色系自然光")
    name, desc = await pick_image_style("手机怎么选", "正文", _llm_returning("科技蓝调"))
    assert name == "科技蓝调"
    assert desc == "深蓝科技光感"


@pytest.mark.asyncio
async def test_llm_invalid_answer_falls_back_to_keyword_scoring():
    await _add_style("科技蓝调", "手机,数码", "深蓝科技光感")
    await _add_style("暖木家居", "家具,装修", "暖木色系自然光")
    # LLM 返回库外名字 → 关键词兜底：query 命中「手机」应选科技蓝调
    name, desc = await pick_image_style("手机选购指南", "正文", _llm_returning("不存在的风格"))
    assert name == "科技蓝调"
    assert desc == "深蓝科技光感"


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_keyword_scoring():
    await _add_style("暖木家居", "家具,装修,客厅", "暖木色系自然光")
    name, desc = await pick_image_style("客厅装修避坑", "家具怎么挑", _llm_raising)
    assert name == "暖木家居"
    assert desc == "暖木色系自然光"


@pytest.mark.asyncio
async def test_empty_user_library_falls_back_to_builtin():
    name, desc = await pick_image_style("手机数码新品测评", "芯片参数对比",
                                        _llm_returning("3D渲染"))
    assert name in _BUILTIN_NAMES
    assert desc == next(d for n, d, _ in IMAGE_STYLE_LIBRARY if n == name)


@pytest.mark.asyncio
async def test_empty_user_library_keyword_fallback_builtin():
    name, desc = await pick_image_style("家居好物推荐", "", _llm_raising)
    assert name == "真实摄影"  # 「家居」命中真实摄影的关键词


@pytest.mark.asyncio
async def test_disabled_entries_not_in_candidates():
    await _add_style("停用风格", "手机", "停用描述", enabled=False)
    # 唯一用户条目已停用 → 视为空库，回退内置
    name, _ = await pick_image_style("手机", "", _llm_returning("停用风格"))
    assert name in _BUILTIN_NAMES


@pytest.mark.asyncio
async def test_style_desc_for_lookup_order():
    await _add_style("科技蓝调", "", "用户库描述词")
    assert await style_desc_for("科技蓝调") == "用户库描述词"      # 用户库优先
    assert await style_desc_for("治愈暖彩") == IMAGE_STYLE_LIBRARY[0][1]  # 内置兜底
    assert await style_desc_for("不存在") is None
    assert await style_desc_for("") is None
    assert await style_desc_for(None) is None
