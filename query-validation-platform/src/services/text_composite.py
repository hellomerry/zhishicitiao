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


def composite_page(image_bytes: bytes, page_body: str, page_index: int,
                   offset: int = 0, banned=None, style: str = None) -> bytes | None:
    """把分页文案用真实字体合成到 AI 背景图上，返回 PNG 字节（1152x1536）。

    字体缺失/图片解析失败返回 None——调用方保留原图，合成失败不阻塞出图。
    offset（2026-09-02 反同质化）：排版样式/文字区槽位随任务偏移，必须与
    生图提示词 get_image_prompt(layout_offset=...) 传同一值，否则 AI 预留的
    留白区与合成落版位置错开。
    banned（2026-09-02 版式禁用）：该页被禁用的槽位集合，与生图提示词
    get_image_prompt(layout_bans=...) 传同一份（slot_for_page 统一顺延）。
    style（2026-09-02 老管线套图跟随）：显式指定版式名（如 classic_pills）
    时跳过槽位轮换，与生图提示词 get_image_prompt(force_slot=...) 配套使用。"""
    paths = _font_paths()
    if paths is None:
        print("[text_composite] 字体缺失（static/fonts/NotoSansSC-*.otf），跳过合成",
              flush=True)
        return None
    try:
        from PIL import Image
        from src.gateway.prompt_versions import slot_for_page
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img = _cover_resize(img)
        title, body = split_title_body(page_body)
        if title or body:
            slot = slot_for_page(page_index, offset, banned)
            _draw_text_block(img, title, body, slot, style=style)
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


# ===== 排版样式（6 页轮换，2026-08-31；2026-09-02 浅色系化）=====
# 2026-09-02 用户决策：永久摒弃两类版式——①深底白字（深色面板/深色渐变蒙版+
# 白字）；②发光字（任何形式的光晕垫底）。全部版式统一为浅色承载+深色文字。
DARK_TEXT = (26, 26, 26)
WHITE_TEXT = (255, 255, 255)

# 样式与页码对应表（与 _ZONE_BY_PAGE 的留白区一一对应，调整轮换只改这里）
_STYLE_BY_PAGE = {
    1: "light_scrim",     # 封面（bottom）：底部浅色渐变蒙版
    2: "light_banner",    # 要点（top）：浅色通栏横幅
    3: "light_panel",     # 特写（bottom）：浅色半透明圆角面板
    4: "magazine_rule",   # 清单（top）：无面板杂志式 大标题+细分隔线
    5: "capsule_tags",    # 场景（top）：浅色胶囊标签
    6: "center_capsule",  # 总结（center）：浅色居中胶囊大标题
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


def _style_light_scrim(img, title: str, body: str):
    """P1 封面（zone=bottom）：底部浅色渐变蒙版——从透明渐变到暖米白的竖向
    渐变带（不是圆角矩形），深色大标题+一行副标题，杂志封面式。
    设计意图：封面要海报感，渐变带压得住任意底图又不像「黑框」那样边界生硬。
    2026-09-02 用户决策：永久摒弃深底白字类版式，蒙版由深色改为暖米白。"""
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
        grad.putpixel((0, yy), (250, 247, 240, round(235 * t)))
    base = img.convert("RGBA")
    base.alpha_composite(grad.resize((W, band_h)), (0, band_top))
    img.paste(base.convert("RGB"), (0, 0))
    draw = ImageDraw.Draw(img)
    y = y_text
    for line in t_lines:
        draw.text((W / 2, y + lh_t / 2), line, font=tf, fill=DARK_TEXT, anchor="mm")
        y += lh_t
    y += gap
    for line in b_lines:
        draw.text((W / 2, y + lh_b / 2), line, font=bf, fill=(60, 58, 54),
                  anchor="mm")
        y += lh_b


def _style_light_banner(img, title: str, body: str):
    """P2 要点页（zone=top）：浅色通栏横幅——贴图顶、撑满图宽的米白横带
    （直角、205 透明），深色文字居中排布。
    设计意图：要点页信息量最大，通栏横带稳定托住整组文字；与 P3 底部圆角
    面板、P5 胶囊标签形成三种不同的浅色承载。
    2026-09-02 用户决策：永久摒弃发光字版式，全系统不再使用任何光晕垫底。"""
    from PIL import Image, ImageDraw
    W, H = img.size
    margin = 96
    pad_v = 44
    probe = ImageDraw.Draw(img)
    tf, bf, t_lines, b_lines, lh_t, lh_b, gap, text_h = _fit_text(
        probe, title, body, W - 2 * margin, int(H * 0.36) - 2 * pad_v, 62, 44)
    band_h = text_h + 2 * pad_v
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rectangle((0, 0, W, band_h),
                                      fill=(255, 255, 255, 205))
    img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"), (0, 0))
    draw = ImageDraw.Draw(img)
    y = pad_v
    for line in t_lines:
        draw.text((W / 2, y + lh_t / 2), line, font=tf, fill=DARK_TEXT, anchor="mm")
        y += lh_t
    y += gap
    for line in b_lines:
        draw.text((W / 2, y + lh_b / 2), line, font=bf, fill=DARK_TEXT, anchor="mm")
        y += lh_b


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


def _style_center_capsule(img, title: str, body: str):
    """P6 总结页（zone=center）：浅色居中胶囊大标题——标题一颗大胶囊、
    正文一颗小胶囊，纵向居中成组，深色文字。
    设计意图：收尾页只要一句结论，居中大字号胶囊干净利落；与 P5 顶部
    小胶囊群的区别：居中、字号更大、最多两颗。
    2026-09-02 用户决策：永久摒弃发光字版式，全系统不再使用任何光晕垫底。"""
    from PIL import Image, ImageDraw
    W, H = img.size
    margin = 96
    pad_h, pad_v, pill_gap = 56, 26, 32
    probe = ImageDraw.Draw(img)
    tf, bf, t_lines, b_lines, lh_t, lh_b, gap, text_h = _fit_text(
        probe, title, body, W - 2 * margin - 2 * pad_h,
        int(H * 0.4) - 2 * pad_v - pill_gap, 84, 48)
    blocks = [(t_lines, tf, lh_t)] if t_lines else []
    if b_lines:
        blocks.append((b_lines, bf, lh_b))
    laid, total_h = [], 0
    for lines, font, lh in blocks:
        w = max(probe.textlength(l, font=font) for l in lines)
        h = len(lines) * lh + 2 * pad_v
        laid.append((lines, font, lh, w, h))
        total_h += h
    total_h += pill_gap * max(len(laid) - 1, 0)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    y = (H - total_h) // 2
    boxes = []
    for lines, font, lh, w, h in laid:
        x0, x1 = (W - w) / 2 - pad_h, (W + w) / 2 + pad_h
        od.rounded_rectangle((x0, y, x1, y + h), radius=h / 2,
                             fill=(255, 255, 255, 215))
        boxes.append((lines, font, lh, y))
        y += h + pill_gap
    img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"), (0, 0))
    draw = ImageDraw.Draw(img)
    for lines, font, lh, y0 in boxes:
        yy = y0 + pad_v
        for line in lines:
            draw.text((W / 2, yy + lh / 2), line, font=font, fill=DARK_TEXT,
                      anchor="mm")
            yy += lh


# 经典彩色药丸的色块配色（淡绿/淡橙交替，复刻 08-26 老套图 AI 画字风格）
_CLASSIC_PILL_COLORS = [(232, 241, 222, 220), (247, 228, 204, 220)]


def _classic_body_segments(body: str) -> tuple:
    """正文拆药丸短句 + 剩余小字注释：按顿号/逗号/句号等切分，前 3 段做药丸
    （去掉句末标点，色块内不保留标点更干净），其余并作底部注释行。"""
    import re
    parts = [p for p in re.split(r"(?<=[、，。；！？])", body or "") if p.strip()]
    pills = [re.sub(r"[、，。；！？]+$", "", p).strip() for p in parts[:3]]
    pills = [p for p in pills if p]
    footer = "".join(parts[3:]).strip() if len(parts) > 3 else ""
    return pills, footer


def _style_classic_pills(img, title: str, body: str):
    """经典彩色药丸（2026-09-02，08-26 老管线套图 AI 画字风格的程序化复刻）：
    顶部黑色粗体大标题 + 正文按短句拆彩色药丸色块（淡绿/淡橙交替、深色字、
    居中纵向排列）+ 超出 3 句的剩余文字作底部小字注释。
    用途：老管线套图的修正重出——文字由真实字体渲染（AI 画字必然偶发异体
    变形，点名也修不正确），同时色块样式与已定型前图保持一致。"""
    from PIL import Image, ImageDraw, ImageFont
    W, H = img.size
    margin = 96
    probe = ImageDraw.Draw(img)
    reg_path, bold_path = _font_paths()
    pills, footer = _classic_body_segments(body)
    # 字号自适应缩小：整体高度超限则缩（最小 6 折）
    for scale in (1.0, 0.92, 0.84, 0.76, 0.68, 0.6):
        tf = ImageFont.truetype(bold_path, int(84 * scale))
        pf = ImageFont.truetype(bold_path, int(52 * scale))
        ff = ImageFont.truetype(reg_path, int(38 * scale))
        lh_t, lh_p, lh_f = int(84 * scale * 1.35), int(52 * scale * 1.4), \
            int(38 * scale * 1.5)
        pad_h, pad_v, pill_gap = int(52 * scale), int(20 * scale), int(28 * scale)
        t_lines = _wrap(probe, title, tf, W - 2 * margin) if title else []
        laid, total_h = [], len(t_lines) * lh_t
        for seg in pills:
            lines = _wrap(probe, seg, pf, W - 2 * margin - 2 * pad_h)
            w = max(probe.textlength(l, font=pf) for l in lines)
            h = len(lines) * lh_p + 2 * pad_v
            laid.append((lines, w, h))
            total_h += h
        total_h += pill_gap * len(laid)  # 标题与首个药丸也有间隔
        f_lines = _wrap(probe, footer, ff, W - 2 * margin) if footer else []
        if f_lines:
            total_h += pill_gap + len(f_lines) * lh_f
        if total_h <= int(H * 0.66) or scale == 0.6:
            break
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    y = int(H * 0.08) + len(t_lines) * lh_t  # 药丸从标题下方开始
    boxes = []
    for i, (lines, w, h) in enumerate(laid):
        y += pill_gap
        x0, x1 = (W - w) / 2 - pad_h, (W + w) / 2 + pad_h
        od.rounded_rectangle((x0, y, x1, y + h), radius=h / 2,
                             fill=_CLASSIC_PILL_COLORS[i % 2])
        boxes.append((lines, y, h))
        y += h
    img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"),
              (0, 0))
    draw = ImageDraw.Draw(img)
    yy = int(H * 0.08)
    for line in t_lines:
        draw.text((W / 2, yy + lh_t / 2), line, font=tf, fill=DARK_TEXT,
                  anchor="mm")
        yy += lh_t
    for lines, y0, h in boxes:
        yy = y0 + pad_v
        for line in lines:
            draw.text((W / 2, yy + lh_p / 2), line, font=pf, fill=DARK_TEXT,
                      anchor="mm")
            yy += lh_p
    if f_lines:
        yy = boxes[-1][1] + boxes[-1][2] + pill_gap if boxes else \
            int(H * 0.08) + len(t_lines) * lh_t + pill_gap
        for line in f_lines:
            draw.text((W / 2, yy + lh_f / 2), line, font=ff, fill=(60, 58, 54),
                      anchor="mm")
            yy += lh_f


# 样式名 → 实现函数（monkeypatch 本表可断言页码分派）
_STYLES = {
    "light_scrim": _style_light_scrim,
    "light_banner": _style_light_banner,
    "light_panel": _style_light_panel,
    "magazine_rule": _style_magazine_rule,
    "capsule_tags": _style_capsule_tags,
    "center_capsule": _style_center_capsule,
    # 不走槽位轮换：老管线套图跟随（composite_page(style=...) 显式指定）
    "classic_pills": _style_classic_pills,
}


def _draw_text_block(img, title: str, body: str, slot: int, style: str = None):
    """按页码分派排版样式（6 页轮换，2026-08-31 用户反馈「全是黑框」；
    2026-09-02 用户决策：永久摒弃深底白字与发光字两类版式，全浅色系）。
    style 显式指定时跳过槽位轮换（老管线套图跟随用 classic_pills）。"""
    _STYLES[style or _STYLE_BY_PAGE.get(slot, "light_banner")](img, title, body)
