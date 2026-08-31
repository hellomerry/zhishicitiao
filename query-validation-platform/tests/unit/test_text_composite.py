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

    # ---- 6 页样式轮换（2026-08-31 用户反馈「全是黑框」）----

    @pytest.mark.parametrize("page_index,style", [
        (1, "gradient_scrim"), (2, "glow_text"), (3, "light_panel"),
        (4, "magazine_rule"), (5, "capsule_tags"), (6, "center_adaptive"),
        (7, "gradient_scrim"),  # 页码取模：第 7 页回到封面样式
    ])
    def test_style_dispatch_per_page(self, monkeypatch, page_index, style):
        called = []
        fake = {name: (lambda n: lambda img, t, b: called.append(n))(name)
                for name in text_composite._STYLES}
        monkeypatch.setattr(text_composite, "_STYLES", fake)
        composite_page(_solid_image(), "标题：正文", page_index)
        assert called == [style]

    def _zone_stats(self, img, x0, y0, x1, y1):
        """区域 (平均亮度, 最暗像素亮度, 最亮像素亮度)。"""
        small = img.crop((x0, y0, x1, y1)).convert("RGB").resize((64, 64))
        data = small.tobytes()
        lums = [0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2]
                for i in range(0, len(data), 3)]
        return sum(lums) / len(lums), min(lums), max(lums)

    def test_glow_text_no_panel_on_bright_bg(self):
        # P2 亮背景：无面板（区域均值仍亮）+ 深色文字（存在暗像素）
        out = composite_page(_solid_image(color=(240, 238, 232)), "标题：正文内容", 2)
        avg, dark, _ = self._zone_stats(Image.open(io.BytesIO(out)), 96, 88, 1056, 500)
        assert avg > 180   # 没有深色面板压住
        assert dark < 90   # 有深色文字

    def test_glow_text_white_on_dark_bg(self):
        # P2 暗背景：白字（存在亮像素）+ 无浅面板（区域均值仍暗）
        out = composite_page(_solid_image(color=(30, 32, 38)), "标题：正文内容", 2)
        avg, _, light = self._zone_stats(Image.open(io.BytesIO(out)), 96, 88, 1056, 500)
        assert avg < 100
        assert light > 200

    def test_gradient_scrim_darkens_bottom_naturally(self):
        # P1 封面：底部渐变蒙版——底边被压暗，且蒙版上缘比底边亮（渐变非色块）
        bright = _solid_image(color=(240, 238, 232))
        img = Image.open(io.BytesIO(composite_page(bright, "大标题：一句钩子", 1)))
        bottom = self._zone_stats(img, 96, 1500, 1056, 1530)[0]
        band_top = self._zone_stats(img, 96, 1000, 1056, 1030)[0]
        assert bottom < 150
        assert band_top > bottom + 30

    def test_light_panel_on_dark_bg(self):
        # P3 特写：浅色面板——暗背景底部面板中心应明显变亮
        out = composite_page(_solid_image(color=(30, 32, 38)), "标题：正文内容", 3)
        avg, _, _ = self._zone_stats(Image.open(io.BytesIO(out)), 300, 1250, 850, 1350)
        assert avg > 180

    def test_capsule_tags_are_light_on_dark_bg(self):
        # P5 场景：浅色胶囊——暗背景顶部出现成片的近白像素
        out = composite_page(_solid_image(color=(30, 32, 38)), "标题：正文一句。正文二句。", 5)
        _, _, light = self._zone_stats(Image.open(io.BytesIO(out)), 96, 88, 1056, 600)
        assert light > 220

    def test_center_adaptive_dark_text_on_bright_bg(self):
        # P6 总结：亮区深字、无面板（中部均值仍亮，存在暗文字像素）
        out = composite_page(_solid_image(color=(240, 238, 232)), "总结：一句结论收尾。", 6)
        avg, dark, _ = self._zone_stats(Image.open(io.BytesIO(out)), 96, 600, 1056, 950)
        assert avg > 180
        assert dark < 90

    # ---- 标点孤行修复（2026-08-31）----

    class _FixedDraw:
        """等宽测量桩：每字 10px。"""
        @staticmethod
        def textlength(s, font=None):
            return 10 * len(s)

    def test_wrap_punctuation_joins_previous_line(self):
        lines = text_composite._wrap(self._FixedDraw, "aaaaaa。bb", None, 60)
        assert lines == ["aaaaaa。", "bb"]  # 「。」并入上一行，不孤行

    def test_wrap_closing_bracket_joins_previous_line(self):
        lines = text_composite._wrap(self._FixedDraw, 'aaaaaa」bb', None, 60)
        assert lines == ["aaaaaa」", "bb"]

    def test_wrap_normal_break_unchanged(self):
        lines = text_composite._wrap(self._FixedDraw, "aaaaaabb", None, 60)
        assert lines == ["aaaaaa", "bb"]

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
