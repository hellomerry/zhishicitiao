"""分页文案占位词剥除（2026-08-31 真实 bug：封面被渲染成「标题」两个大字）。"""
from src.pipeline.nodes import _strip_placeholder_lines


def test_strip_placeholder_title_line():
    pages = ["标题\n天山翠和翡翠，到底差在哪？一篇讲清", "成分不同\n硬玉与石英质玉"]
    out = _strip_placeholder_lines(pages)
    assert out[0] == "天山翠和翡翠，到底差在哪？一篇讲清"
    assert out[1] == "成分不同\n硬玉与石英质玉"  # 非占位词原样保留


def test_strip_all_placeholder_words():
    pages = ["副标题：\n一句钩子", "正文\n要点一", "小标题\n要点二"]
    out = _strip_placeholder_lines(pages)
    assert out == ["一句钩子", "要点一", "要点二"]


def test_page_becomes_empty_returns_empty():
    # 整页只有占位词 → 剥完为空串（调用方据此回退机械切割）
    assert _strip_placeholder_lines(["标题"]) == [""]


def test_no_placeholder_unchanged():
    pages = ["高脚杯冰牛奶\n低成本喝出氛围感"]
    assert _strip_placeholder_lines(pages) == pages
