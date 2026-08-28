from src.gateway.prompt_versions import (FIXED_IMAGE_STYLE_SENTENCE,
                                         get_draft_prompt, get_image_prompt)


def test_draft_prompt_per_mode():
    assert "对比" in get_draft_prompt("compare")
    assert "单品" in get_draft_prompt("single")
    assert "图文内容" in get_draft_prompt("general")


def test_draft_prompt_unknown_mode_falls_back_to_general():
    assert get_draft_prompt("nope") == get_draft_prompt("general")


def test_image_prompt_fills_page_body():
    p = get_image_prompt("general", "本页是测试文案")
    assert "本页是测试文案" in p
    assert "{page_body}" not in p


def test_image_prompt_per_mode():
    assert "两个主体" in get_image_prompt("compare", "")
    assert "参考实景图" in get_image_prompt("single", "")
    assert "无参考图" in get_image_prompt("general", "")


# ---- 风格自适应（2026-08-28，迁移 011）：style_desc 替换固定风格句 ----

def test_image_prompt_style_desc_replaces_fixed_sentence():
    p = get_image_prompt("general", "文案", style_desc="深蓝科技光感、几何线条")
    assert "深蓝科技光感、几何线条；" in p
    assert FIXED_IMAGE_STYLE_SENTENCE not in p
    # 主体锚定硬性要求仍在（风格词仅作光影色调氛围）
    assert "仅作光影色调氛围" in p


def test_image_prompt_style_desc_notext_mode():
    p = get_image_prompt("general", "文案", no_text=True, style_desc="扁平矢量插画、大色块")
    assert "扁平矢量插画、大色块；" in p
    assert FIXED_IMAGE_STYLE_SENTENCE not in p


def test_image_prompt_style_desc_none_keeps_fixed_sentence():
    p = get_image_prompt("general", "文案", style_desc=None)
    assert FIXED_IMAGE_STYLE_SENTENCE in p


def test_image_prompt_style_desc_custom_template_without_fixed_sentence():
    # 自定义模板不含固定风格句时不强行注入
    p = get_image_prompt("general", "文案", template="自定义模板 {page_body}",
                         style_desc="任意描述词")
    assert "任意描述词" not in p


def test_image_prompt_style_desc_custom_template_with_fixed_sentence():
    tpl = "自定义前缀。" + FIXED_IMAGE_STYLE_SENTENCE + "后缀 {page_body}"
    p = get_image_prompt("general", "文案", template=tpl, style_desc="手绘线稿、水彩淡彩")
    assert "手绘线稿、水彩淡彩；" in p
    assert FIXED_IMAGE_STYLE_SENTENCE not in p
