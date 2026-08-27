"""文字后期合成器单测（2026-08-27 终极方案）。

真实字体文件（static/fonts/NotoSansSC-*.otf）需在仓库内，合成用 PIL 本地计算，
不触网。字体缺失的降级路径用 monkeypatch 模拟。
"""
import io
import pytest
from PIL import Image

from src.services import text_composite
from src.services.text_composite import composite_page, split_title_body


def _solid_image(w=1200, h=1600, color=(235, 230, 220)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


class TestSplitTitleBody:
    def test_newline_split(self):
        assert split_title_body("大标题\n正文内容") == ("大标题", "正文内容")

    def test_bar_split(self):
        assert split_title_body("小标题｜正文内容") == ("小标题", "正文内容")

    def test_colon_split(self):
        assert split_title_body("办学基础概况：八中1990年创办") == \
            ("办学基础概况", "八中1990年创办")

    def test_first_sentence_split(self):
        assert split_title_body("师资特色。八中师资稳定") == ("师资特色。", "八中师资稳定")

    def test_no_separator_all_body(self):
        # 拆不出标题时全部作正文，宁缺毋滥（硬切会把一句话腰斩）
        assert split_title_body("住城关看重老牌积淀选八中") == \
            ("", "住城关看重老牌积淀选八中")

    def test_long_head_no_split(self):
        # 「标题」超长不当作标题（防止正文首段被误判）
        t, b = split_title_body("这是一个非常非常长的不像标题的开头部分文字｜后面是正文")
        assert (t, b) == ("", "这是一个非常非常长的不像标题的开头部分文字｜后面是正文")

    def test_empty(self):
        assert split_title_body("") == ("", "")
        assert split_title_body(None) == ("", "")


class TestCompositePage:
    @pytest.mark.parametrize("page_index", [1, 2, 3, 4, 5, 6])
    def test_all_page_layouts(self, page_index):
        out = composite_page(_solid_image(),
                             "小标题：正文内容一句话，包含数字1990和310亩。",
                             page_index)
        assert out, f"page {page_index} composite failed"
        img = Image.open(io.BytesIO(out))
        assert img.size == (1152, 1536)
        assert out != _solid_image()

    def test_contrast_dark_panel_on_bright_bg(self):
        # 亮背景 → 深面板：文字区平均亮度应明显低于原背景
        bright = _solid_image(color=(240, 238, 232))
        out = composite_page(bright, "标题：正文", 2)  # page 2 = top zone
        img = Image.open(io.BytesIO(out))
        cx, cy = 576, 200  # 顶部面板中心
        r, g, b = img.getpixel((cx, cy))[:3]
        assert 0.299 * r + 0.587 * g + 0.114 * b < 120

    def test_contrast_light_panel_on_dark_bg(self):
        dark = _solid_image(color=(30, 32, 38))
        out = composite_page(dark, "标题：正文", 2)
        img = Image.open(io.BytesIO(out))
        r, g, b = img.getpixel((576, 200))[:3]
        assert 0.299 * r + 0.587 * g + 0.114 * b > 150

    def test_nonstandard_size_normalized(self):
        # 非 3:4 输入也归一到 1152x1536（与交付导出尺寸一致）
        out = composite_page(_solid_image(800, 800), "标题：正文", 1)
        assert Image.open(io.BytesIO(out)).size == (1152, 1536)

    def test_empty_body_returns_normalized_bg(self):
        out = composite_page(_solid_image(), "", 1)
        assert Image.open(io.BytesIO(out)).size == (1152, 1536)

    def test_missing_font_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(text_composite, "FONT_DIR", tmp_path)
        text_composite._font_paths.cache_clear()
        try:
            assert composite_page(_solid_image(), "标题：正文", 1) is None
        finally:
            text_composite._font_paths.cache_clear()

    def test_corrupt_image_returns_none(self):
        assert composite_page(b"not an image", "标题：正文", 1) is None

    def test_long_copy_shrinks_to_fit(self):
        long_body = "要点：" + "这是一段很长的正文内容，用来验证自动缩字号落版。" * 8
        out = composite_page(_solid_image(), long_body, 2)
        assert out and Image.open(io.BytesIO(out)).size == (1152, 1536)
