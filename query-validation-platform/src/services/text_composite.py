"""文字后期合成（终极方案，2026-08-27）：AI 只画无字背景，分页文案用真实字体
（Noto Sans SC）程序化合成到图上。

根因：gpt-image 中文渲染会混入日文新字体/异体字形（実/対/変），OCR 转写质检又会
脑补纠错、对异体变形是盲区。合成后图上每个汉字都是 Unicode 字符的真实字形，
从根上消除异体变形/伪汉字/乱码。

合成图固定输出 1152x1536（与交付导出归一尺寸一致）。每页文字区位置
（_ZONE_BY_PAGE）与生图提示词 _PAGE_LAYOUTS_NOTEXT 要求 AI 预留的留白区一一对应，
改动时必须两边同步。
"""
import io
from functools import lru_cache
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parents[2] / "static" / "fonts"
OUT_W, OUT_H = 1152, 1536

# 每页文字区位置：对应 _PAGE_LAYOUTS_NOTEXT 的留白区（封面底部/要点顶部/特写底部/
# 清单顶部/场景顶部/总结居中）
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
            _draw_text_block(img, title, body,
                             _ZONE_BY_PAGE.get(slot, "top"), is_cover=(slot == 1))
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


def _wrap(draw, text: str, font, max_w: int) -> list:
    """CJK 逐字换行：逐字累加测量宽度，超宽即断行。"""
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if cur and draw.textlength(cur + ch, font=font) > max_w:
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


def _draw_text_block(img, title: str, body: str, zone: str, is_cover: bool):
    """半透明圆角面板 + 居中排版文字。面板深/浅按文字区背景亮度自适应。"""
    from PIL import Image, ImageDraw, ImageFont
    reg_path, bold_path = _font_paths()
    W, H = img.size
    margin, pad_h, pad_v = 64, 48, 44
    panel_w = W - 2 * margin
    text_w = panel_w - 2 * pad_h
    max_h = int(H * (0.44 if is_cover else 0.36))

    probe = ImageDraw.Draw(img)
    # 从大到小试字号，直到文字块放得下（最小 6 折，保证极端长文案也能落版）
    for scale in (1.0, 0.92, 0.84, 0.76, 0.68, 0.6):
        title_size = int((96 if is_cover else 62) * scale)
        body_size = int((52 if is_cover else 44) * scale)
        tf = ImageFont.truetype(bold_path, title_size)
        bf = ImageFont.truetype(reg_path, body_size)
        t_lines = _wrap(probe, title, tf, text_w) if title else []
        b_lines = _wrap(probe, body, bf, text_w) if body else []
        lh_t, lh_b = int(title_size * 1.4), int(body_size * 1.55)
        gap = 28 if (t_lines and b_lines) else 0
        text_h = len(t_lines) * lh_t + len(b_lines) * lh_b + gap
        if text_h <= max_h - 2 * pad_v or scale == 0.6:
            break

    panel_h = text_h + 2 * pad_v
    x0, x1 = margin, W - margin
    if zone == "top":
        y0 = margin + 24
    elif zone == "bottom":
        y0 = H - margin - 24 - panel_h
    else:  # center
        y0 = (H - panel_h) // 2
    y1 = y0 + panel_h

    if _zone_luminance(img, x0, y0, x1, y1) > 140:
        panel_fill, text_fill = (16, 16, 20, 168), (255, 255, 255)
    else:
        panel_fill, text_fill = (255, 255, 255, 178), (26, 26, 26)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rounded_rectangle((x0, y0, x1, y1), radius=36,
                                              fill=panel_fill)
    base = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    img.paste(base, (0, 0))

    draw = ImageDraw.Draw(img)
    y = y0 + pad_v
    for line in t_lines:
        draw.text((W / 2, y + lh_t / 2), line, font=tf, fill=text_fill, anchor="mm")
        y += lh_t
    y += gap
    for line in b_lines:
        draw.text((W / 2, y + lh_b / 2), line, font=bf, fill=text_fill, anchor="mm")
        y += lh_b
