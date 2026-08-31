"""文字后期合成（终极方案，2026-08-27）：AI 只画无字背景，分页文案用真实字体
（Noto Sans SC）程序化合成到图上。

根因：gpt-image 中文渲染会混入日文新字体/异体字形（実/対/変），OCR 转写质检又会
脑补纠错、对异体变形是盲区。合成后图上每个汉字都是 Unicode 字符的真实字形，
从根上消除异体变形/伪汉字/乱码。

合成图固定输出 1152x1536（与交付导出归一尺寸一致）。每页文字区位置
（_ZONE_BY_PAGE）与生图提示词 _PAGE_LAYOUTS_NOTEXT 要求 AI 预留的留白区一一对应，
改动时必须两边同步。

排版样式 6 页轮换（2026-08-31 用户反馈「文字出现形式单一，全是黑框压在上面」）：
原实现只有「半透明深色圆角面板」一种形式，写实静物类图背景偏亮，每页都被压上
深灰框。现按页码分派 6 种样式（_STYLE_BY_PAGE → _STYLES），见各样式函数注释。
"""
import io
from functools import lru_cache
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parents[2] / "static" / "fonts"
OUT_W, OUT_H = 1152, 1536

# 每页文字区位置：对应 _PAGE_LAYOUTS_NOTEXT 的留白区（封面底部/要点顶部/特写底部/
# 清单顶部/场景顶部/总结居中）；各样式函数（_style_*）内部 zone 假设与本表一致
_ZONE_BY_PAGE = {1: "bottom", 2: "top", 3: "bottom", 4: "top", 5: "top", 6: "center"}


@lru_cache(maxsize=1)
def _font_paths() -> tuple | None:
    reg = FONT_DIR / "NotoSansSC-Regular.otf"
    bold = FONT_DIR / "NotoSansSC-Bold.otf"
    if reg.exists() and bold.exists():
        return str(reg), str(bold)
    return None


def split_title_body(page_body: str) -> tuple:
    """分页文案拆（标题, 正文）。分页文案是「小标题+正文」单段纯文本，
    优先按换行/｜/：拆，其次首句（≤20字）作标题；拆不出则全部作正文，宁缺毋滥
    （硬切可能把一句话腰斩）。"""
    import re
    text = re.sub(r"\s+\n", "\n", (page_body or "").strip())
    if not text:
        return "", ""
    for sep in ("\n", "｜", "|", "："):
        if sep in text:
            head, _, rest = text.partition(sep)
            if head.strip() and rest.strip() and len(head.strip()) <= 20:
                return head.strip(), rest.strip()
    m = re.match(r"^(.{2,20}?[。！？])(.+)$", text, flags=re.S)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", text


def composite_page(image_bytes: bytes, page_body: str, page_index: int) -> bytes | None:
    """把分页文案用真实字体合成到 AI 背景图上，返回 PNG 字节（1152x1536）。

    字体缺失/图片解析失败返回 None——调用方保留原图，合成失败不阻塞出图。
    """
    paths = _font_paths()
    if paths is None:
        print("[text_composite] 字体缺失（static/fonts/NotoSansSC-*.otf），跳过合成",
              flush=True)
        return None
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img = _cover_resize(img)
        title, body = split_title_body(page_body)
        if title or body:
            slot = ((page_index - 1) % 6) + 1
            _draw_text_block(img, title, body, slot)
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        return None


def _cover_resize(img):
    """等比放大到覆盖 1152x1536 后居中裁剪（不改变画面构图比例）。"""
    from PIL import Image
    w, h = img.size
    scale = max(OUT_W / w, OUT_H / h)
    img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
    w, h = img.size
    left, top = (w - OUT_W) // 2, (h - OUT_H) // 2
    return img.crop((left, top, left + OUT_W, top + OUT_H))


# 禁止出现在行首的中文人名标点/闭合符：断行处遇到它们时并入上一行
_NO_LINE_START = set("，。、；：？！\"」』）")


def _wrap(draw, text: str, font, max_w: int) -> list:
    """CJK 逐字换行：逐字累加测量宽度，超宽即断行。
    标点孤行修复（2026-08-31）：断行处若是中文标点/闭合括号引号，把该字符并入
    上一行（允许上一行略超宽），避免「。」之类独占一行或挂在行首。"""
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if cur and ch not in _NO_LINE_START \
                and draw.textlength(cur + ch, font=font) > max_w:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def _zone_luminance(img, x0: int, y0: int, x1: int, y1: int) -> float:
    """文字区背景平均亮度（0-255），用于选深/浅面板保证文字对比度。"""
    region = img.crop((x0, y0, x1, y1)).resize((1, 1))
    r, g, b = region.getpixel((0, 0))[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b


# ===== 排版样式（6 页轮换，2026-08-31）=====
# 文字颜色统一规则：排版区背景亮 → 深色文字；暗 → 白色文字；
# 有面板的样式面板色随之配套。
DARK_TEXT = (26, 26, 26)
WHITE_TEXT = (255, 255, 255)

# 样式与页码对应表（与 _ZONE_BY_PAGE 的留白区一一对应，调整轮换只改这里）
_STYLE_BY_PAGE = {
    1: "gradient_scrim",   # 封面（bottom）：底部渐变蒙版
    2: "glow_text",        # 要点（top）：无面板文字+柔和外发光
    3: "light_panel",      # 特写（bottom）：浅色半透明圆角面板
    4: "magazine_rule",    # 清单（top）：无面板杂志式 大标题+细分隔线
    5: "capsule_tags",     # 场景（top）：浅色胶囊标签
    6: "center_adaptive",  # 总结（center）：无面板居中大标题，颜色亮度自适应
}


def _text_color_for(lum: float) -> tuple:
    """文字颜色统一规则：排版区背景亮 → 深色文字；暗 → 白色文字。"""
    return DARK_TEXT if lum > 140 else WHITE_TEXT


def _fit_text(probe, title: str, body: str, text_w: int, max_h: int,
              title_size0: int, body_size0: int) -> tuple:
    """字号自适应缩小（保留原 scale 循环）：从大到小试，直到文字块放得下
    （最小 6 折，保证极端长文案也能落版）。返回字体/分行/行高/总高。"""
    from PIL import ImageFont
    reg_path, bold_path = _font_paths()
    for scale in (1.0, 0.92, 0.84, 0.76, 0.68, 0.6):
        title_size = int(title_size0 * scale)
        body_size = int(body_size0 * scale)
        tf = ImageFont.truetype(bold_path, title_size)
        bf = ImageFont.truetype(reg_path, body_size)
        t_lines = _wrap(probe, title, tf, text_w) if title else []
        b_lines = _wrap(probe, body, bf, text_w) if body else []
        lh_t, lh_b = int(title_size * 1.4), int(body_size * 1.55)
        gap = 28 if (t_lines and b_lines) else 0
        text_h = len(t_lines) * lh_t + len(b_lines) * lh_b + gap
        if text_h <= max_h or scale == 0.6:
            break
    return tf, bf, t_lines, b_lines, lh_t, lh_b, gap, text_h


def _draw_lines_glow(img, items: list, glow_fill: tuple, radius: int = 8):
    """给整组文字垫柔和外发光：先把文字画到透明层、一次高斯模糊成均匀晕开的
    光晕垫底，再画实体文字（不是描边也不是阴影，边缘没有生硬轮廓）。
    items = [(中心xy, 行文本, 字体, 文字颜色)]，glow_fill 为发光色（RGB）。"""
    from PIL import Image, ImageDraw, ImageFilter
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for xy, line, font, _fill in items:
        d.text(xy, line, font=font, fill=glow_fill + (230,), anchor="mm")
    layer = layer.filter(ImageFilter.GaussianBlur(radius))
    img.paste(Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB"), (0, 0))
    d = ImageDraw.Draw(img)
    for xy, line, font, fill in items:
        d.text(xy, line, font=font, fill=fill, anchor="mm")


def _style_gradient_scrim(img, title: str, body: str):
    """P1 封面（zone=bottom）：底部渐变蒙版——从透明渐变到深色的竖向渐变带
    （不是圆角矩形），白色大标题+一行副标题，杂志封面式。
    设计意图：封面要海报感，渐变带压得住任意底图又不像「黑框」那样边界生硬。"""
    from PIL import Image, ImageDraw
    W, H = img.size
    margin = 96
    probe = ImageDraw.Draw(img)
    tf, bf, t_lines, b_lines, lh_t, lh_b, gap, text_h = _fit_text(
        probe, title, body, W - 2 * margin, int(H * 0.30), 96, 52)
    y_text = H - 96 - text_h  # 文字块底距图底 96
    band_top = max(0, y_text - 160)  # 文字上方再留一段透明过渡
    band_h = H - band_top
    grad = Image.new("RGBA", (1, band_h))
    for yy in range(band_h):
        t = (yy / max(band_h - 1, 1)) ** 1.6  # 缓入，过渡更自然
        grad.putpixel((0, yy), (10, 10, 14, round(215 * t)))
    base = img.convert("RGBA")
    base.alpha_composite(grad.resize((W, band_h)), (0, band_top))
    img.paste(base.convert("RGB"), (0, 0))
    draw = ImageDraw.Draw(img)
    y = y_text
    for line in t_lines:
        draw.text((W / 2, y + lh_t / 2), line, font=tf, fill=WHITE_TEXT, anchor="mm")
        y += lh_t
    y += gap
    for line in b_lines:
        draw.text((W / 2, y + lh_b / 2), line, font=bf, fill=(235, 235, 235),
                  anchor="mm")
        y += lh_b


def _style_glow_text(img, title: str, body: str):
    """P2 要点页（zone=top）：无面板，文字直接排版在预留留白区；为保证可读性
    垫一层反向柔和外发光（亮区深字+白光晕，暗区白字+暗光晕）。
    设计意图：要点页信息量最大，去掉面板让画面通透，光晕只托住文字边缘。"""
    from PIL import ImageDraw
    W, H = img.size
    margin = 96
    probe = ImageDraw.Draw(img)
    tf, bf, t_lines, b_lines, lh_t, lh_b, gap, text_h = _fit_text(
        probe, title, body, W - 2 * margin, int(H * 0.36), 62, 44)
    y0 = margin + 24
    fill = _text_color_for(_zone_luminance(img, margin, y0, W - margin, y0 + text_h))
    glow = WHITE_TEXT if fill == DARK_TEXT else DARK_TEXT
    items, y = [], y0
    for line in t_lines:
        items.append(((W / 2, y + lh_t / 2), line, tf, fill))
        y += lh_t
    y += gap
    for line in b_lines:
        items.append(((W / 2, y + lh_b / 2), line, bf, fill))
        y += lh_b
    _draw_lines_glow(img, items, glow)


def _style_light_panel(img, title: str, body: str):
    """P3 特写页（zone=bottom）：浅色半透明（米白 255,255,255,190）圆角面板
    +深色文字。设计意图：特写页主体充满全图，底部横条需要稳定托底，但用浅色
    面板取代原来的深灰框，避免「黑框压图」。"""
    from PIL import Image, ImageDraw
    W, H = img.size
    margin, pad_h, pad_v = 64, 48, 40
    probe = ImageDraw.Draw(img)
    tf, bf, t_lines, b_lines, lh_t, lh_b, gap, text_h = _fit_text(
        probe, title, body, W - 2 * margin - 2 * pad_h,
        int(H * 0.36) - 2 * pad_v, 62, 44)
    panel_h = text_h + 2 * pad_v
    x0, x1 = margin, W - margin
    y0 = H - margin - 24 - panel_h
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rounded_rectangle(
        (x0, y0, x1, y0 + panel_h), radius=36, fill=(255, 255, 255, 190))
    img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"), (0, 0))
    draw = ImageDraw.Draw(img)
    y = y0 + pad_v
    for line in t_lines:
        draw.text((W / 2, y + lh_t / 2), line, font=tf, fill=DARK_TEXT, anchor="mm")
        y += lh_t
    y += gap
    for line in b_lines:
        draw.text((W / 2, y + lh_b / 2), line, font=bf, fill=DARK_TEXT, anchor="mm")
        y += lh_b


def _style_magazine_rule(img, title: str, body: str):
    """P4 清单页（zone=top）：无面板杂志式——深色大标题+一条细分隔线+正文。
    设计意图：清单页信息分块多，用编辑排版（标题/分隔线/正文）取代面板，
    画面更轻；文字与分隔线颜色按排版区亮度自适应。"""
    from PIL import ImageDraw
    W, H = img.size
    margin = 96
    probe = ImageDraw.Draw(img)
    tf, bf, t_lines, b_lines, lh_t, lh_b, gap, text_h = _fit_text(
        probe, title, body, W - 2 * margin, int(H * 0.36), 66, 44)
    y0 = margin + 24
    fill = _text_color_for(_zone_luminance(img, margin, y0, W - margin, y0 + text_h))
    draw = ImageDraw.Draw(img)
    y = y0
    for line in t_lines:
        draw.text((W / 2, y + lh_t / 2), line, font=tf, fill=fill, anchor="mm")
        y += lh_t
    if t_lines and b_lines:
        # 细分隔线：标题与正文之间的杂志式分隔（宽 320px、3px 粗）
        ry = y + gap / 2
        draw.rectangle((W / 2 - 160, ry - 1, W / 2 + 160, ry + 2), fill=fill)
        y += gap
    for line in b_lines:
        draw.text((W / 2, y + lh_b / 2), line, font=bf, fill=fill, anchor="mm")
        y += lh_b


def _capsule_sentences(body: str) -> list:
    """正文按句拆胶囊：按句末标点切分，超过 2 句时后部并入第 2 个胶囊。"""
    import re
    parts = [p for p in re.split(r"(?<=[。！？；])", body or "") if p.strip()]
    if len(parts) > 2:
        parts = parts[:1] + ["".join(parts[1:])]
    return parts[:2]


def _style_capsule_tags(img, title: str, body: str):
    """P5 场景页（zone=top）：胶囊形圆角标签（浅色底深色字），标题一个胶囊、
    正文按句拆 1-2 个短句胶囊，纵向排列留间距。
    设计意图：场景页全幅插画，文字拆成小胶囊「贴」在留白区，比整块面板轻。"""
    from PIL import Image, ImageDraw, ImageFont
    W, H = img.size
    margin = 96
    text_w = W - 2 * margin
    pad_h, pad_v, cap_gap = 40, 18, 20
    segs = ([("title", title)] if title else []) + \
        [("body", s) for s in _capsule_sentences(body)]
    probe = ImageDraw.Draw(img)
    reg_path, bold_path = _font_paths()
    # 字号自适应缩小：所有胶囊总高超限则整体缩（最小 6 折）
    for scale in (1.0, 0.92, 0.84, 0.76, 0.68, 0.6):
        tf = ImageFont.truetype(bold_path, int(56 * scale))
        bf = ImageFont.truetype(reg_path, int(40 * scale))
        lh_t, lh_b = int(56 * scale * 1.4), int(40 * scale * 1.5)
        laid, total_h = [], 0
        for kind, seg in segs:
            font, lh = (tf, lh_t) if kind == "title" else (bf, lh_b)
            lines = _wrap(probe, seg, font, text_w - 2 * pad_h)
            w = max(probe.textlength(l, font=font) for l in lines)
            h = len(lines) * lh + 2 * pad_v
            laid.append((lines, font, lh, w, h))
            total_h += h
        total_h += cap_gap * max(len(laid) - 1, 0)
        if total_h <= int(H * 0.4) or scale == 0.6:
            break
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    boxes, y = [], margin + 24
    for lines, font, lh, w, h in laid:
        x0, x1 = (W - w) / 2 - pad_h, (W + w) / 2 + pad_h
        od.rounded_rectangle((x0, y, x1, y + h), radius=h / 2,
                             fill=(255, 255, 255, 210))
        boxes.append((lines, font, lh, y))
        y += h + cap_gap
    img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"), (0, 0))
    draw = ImageDraw.Draw(img)
    for lines, font, lh, y0 in boxes:
        yy = y0 + pad_v
        for line in lines:
            draw.text((W / 2, yy + lh / 2), line, font=font, fill=DARK_TEXT,
                      anchor="mm")
            yy += lh


def _style_center_adaptive(img, title: str, body: str):
    """P6 总结页（zone=center）：无面板居中大标题+充足留白；文字颜色按该区域
    亮度自适应（亮区深字/暗区白字），垫反向柔和外发光保证可读。
    设计意图：收尾页只要一句结论，居中大字号+大量留白，干净利落。"""
    from PIL import ImageDraw
    W, H = img.size
    margin = 96
    probe = ImageDraw.Draw(img)
    tf, bf, t_lines, b_lines, lh_t, lh_b, gap, text_h = _fit_text(
        probe, title, body, W - 2 * margin, int(H * 0.4), 84, 48)
    y0 = (H - text_h) // 2
    fill = _text_color_for(_zone_luminance(img, margin, y0, W - margin, y0 + text_h))
    glow = WHITE_TEXT if fill == DARK_TEXT else DARK_TEXT
    items, y = [], y0
    for line in t_lines:
        items.append(((W / 2, y + lh_t / 2), line, tf, fill))
        y += lh_t
    y += gap
    for line in b_lines:
        items.append(((W / 2, y + lh_b / 2), line, bf, fill))
        y += lh_b
    _draw_lines_glow(img, items, glow)


# 样式名 → 实现函数（monkeypatch 本表可断言页码分派）
_STYLES = {
    "gradient_scrim": _style_gradient_scrim,
    "glow_text": _style_glow_text,
    "light_panel": _style_light_panel,
    "magazine_rule": _style_magazine_rule,
    "capsule_tags": _style_capsule_tags,
    "center_adaptive": _style_center_adaptive,
}


def _draw_text_block(img, title: str, body: str, slot: int):
    """按页码分派排版样式（6 页轮换，2026-08-31 用户反馈「全是黑框」）：
    原实现只有「半透明深色圆角面板」一种，亮背景图每页都是深色框。"""
    _STYLES[_STYLE_BY_PAGE.get(slot, "glow_text")](img, title, body)
