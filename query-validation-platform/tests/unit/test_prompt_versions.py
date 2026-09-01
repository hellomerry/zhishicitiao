from src.gateway.prompt_versions import (FIXED_IMAGE_STYLE_SENTENCE,
                                         SUBJECT_ANCHOR_SENTENCE,
                                         _TEXT_PRESENTATIONS,
                                         get_draft_prompt, get_image_prompt)


def test_draft_prompt_per_mode():
    assert "对比" in get_draft_prompt("compare")
    assert "单品" in get_draft_prompt("single")
    assert "科普/教程类图文" in get_draft_prompt("general")


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


# ---- 动态主体锚定（2026-08-31）：page_subject 替换锚定条款里的通用例子句 ----

def test_image_prompt_page_subject_replaces_anchor():
    p = get_image_prompt("general", "文案", page_subject="加冰块的高脚杯牛奶")
    assert "本页画面主体必须是：加冰块的高脚杯牛奶，占据画面视觉中心" in p
    assert SUBJECT_ANCHOR_SENTENCE not in p
    # 风格词仅作氛围的约束保留
    assert "仅作光影色调氛围" in p


def test_image_prompt_page_subject_notext_mode():
    p = get_image_prompt("general", "文案", no_text=True,
                         page_subject="两只碰杯的手特写")
    assert "本页画面主体必须是：两只碰杯的手特写，占据画面视觉中心" in p
    assert SUBJECT_ANCHOR_SENTENCE not in p


def test_image_prompt_page_subject_custom_template_with_anchor():
    # 自定义模板含同样锚定句时也应替换
    tpl = "自定义前缀。" + SUBJECT_ANCHOR_SENTENCE + "后缀 {page_body}"
    p = get_image_prompt("general", "文案", template=tpl, page_subject="一杯热拿铁")
    assert "本页画面主体必须是：一杯热拿铁" in p
    assert SUBJECT_ANCHOR_SENTENCE not in p


def test_image_prompt_page_subject_none_keeps_anchor():
    p = get_image_prompt("general", "文案", page_subject=None)
    assert SUBJECT_ANCHOR_SENTENCE in p
    p2 = get_image_prompt("general", "文案", no_text=True, page_subject="")
    assert SUBJECT_ANCHOR_SENTENCE in p2


def test_image_prompt_page_subject_custom_template_without_anchor():
    # 自定义模板不含锚定句时不强行注入
    p = get_image_prompt("general", "文案", template="自定义模板 {page_body}",
                         page_subject="一杯热拿铁")
    assert "一杯热拿铁" not in p


# ---- 文字呈现形式分页轮换（2026-08-31）：有字版按页注入，无字版不注入 ----

def test_image_prompt_text_presentation_rotates_per_page():
    for i in range(1, 7):
        p = get_image_prompt("general", "文案", page_index=i)
        assert _TEXT_PRESENTATIONS[i - 1] in p
    # 页码取模：第 7 页回到第 1 条
    assert _TEXT_PRESENTATIONS[0] in get_image_prompt("general", "文案", page_index=7)


def test_image_prompt_text_presentation_not_in_notext_mode():
    p = get_image_prompt("general", "文案", page_index=1, no_text=True)
    assert "文字呈现形式" not in p


def test_image_prompt_dark_box_limit_present():
    # 全局约束：深色（近黑/深灰）底文字框全套最多 1 次；
    # 主题色彩色胶囊标签为推荐形式、豁免不计入（2026-09-01 人工样例修正）
    p = get_image_prompt("general", "文案")
    assert "深色（近黑/深灰）底文字框最多出现1次" in p
    assert "彩色胶囊" in p and "不算深色框" in p


# ---- 通用模式实景图（2026-09-01）：has_refs 切换 general 前缀两版 ----

def test_image_prompt_general_with_refs_swaps_prefix():
    p = get_image_prompt("general", "文案", has_refs=True)
    assert "将提供的参考实景图融入画面" in p
    assert "纯 AI 生成" not in p
    # 无实图回退纯 AI 版
    p2 = get_image_prompt("general", "文案")
    assert "纯 AI 生成、无参考图" in p2


def test_image_prompt_general_with_refs_notext_mode():
    p = get_image_prompt("general", "文案", no_text=True, has_refs=True)
    assert "将提供的参考实景图融入画面" in p
    assert "纯 AI 生成" not in p
    p2 = get_image_prompt("general", "文案", no_text=True)
    assert "纯 AI 生成、无参考图" in p2


def test_image_prompt_general_custom_template_not_forced():
    # 自定义模板不含无参考图前缀时不强行注入
    p = get_image_prompt("general", "文案", template="自定义模板 {page_body}",
                         has_refs=True)
    assert "将提供的参考实景图融入画面" not in p


def test_image_prompt_other_modes_unaffected_by_has_refs():
    # compare/single 模板本就含参考图措辞，has_refs 不改变它们
    assert "参考实景图" in get_image_prompt("compare", "文案", has_refs=True)
    assert "参考实景图" in get_image_prompt("single", "文案", has_refs=False)
