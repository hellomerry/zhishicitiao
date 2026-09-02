"""场景化视觉扩写器（2026-09-02，移植 8003 栈 visual_writer 的实测思路）。

背景：8003 生图风格实测优于 8000，核心差距不在生图模型，而在生图前多一层
「视觉导演」——LLM 把 6 页中文文案扩写成丰富的英文视觉描述（复刻 ChatGPT
网页端的 prompt 增强层），gpt-image-2 对英文视觉指令的理解显著更准。

与 8003 的两处关键差异（适配 8000 管线）：
- 8000 的文字由程序后期合成（AI 只画无字背景），所以扩写只覆盖画面视觉，
  明确禁止画面出现文字；8003 的带字装置（横幅白条/序号徽章/判决贴纸）
  替换为无字装置（浅色色块/装饰图形/圆角内嵌照片/虚线箭头等）；
- 跨任务记忆用 DB 笔记池（visual_notes 表）实现，不依赖 nanobot 外部服务：
  审核驳回理由/配图修正标记自动沉淀，每次扩写注入最近若干条。

产出冻结到 tasks.visual_json（迁移 020），定点重生成沿用快照不重扩写
（与 014 主体快照/015 风格快照/017 策划快照同一冻结哲学）。
任何失败返回 None，调用方回退现有中文无字骨架，不阻塞出图。
"""
import json
import traceback

from sqlalchemy import text

_VISUAL_PROMPT = """You are the visual director for a Chinese social-media 6-page info card set. Turn the 6 pages of Chinese on-image copy into rich ENGLISH visual directions for the image model (gpt-image-2).

CRITICAL CONTEXT: the images you describe are TEXT-FREE BACKGROUNDS. All typography is composited later by software. Every page must contain NO text, NO letters, NO numbers, NO watermark. Describe only the visual scene.

Unified style chosen for this set (Chinese, follow its spirit):
【style】{style_name}：{style_desc}

Recent reviewer feedback notes (learn from them, avoid repeating mistakes; colour-related feedback deserves EXTRA attention):
{notes}

COLOUR DIRECTION (decide it yourself, per topic):
- Analyse the topic's mood and pick: (a) the background tint for all 6 pages, (b) the decorative accent palette. They must harmonize; keep low-saturation, comfortable, restrained.
- Reference palette of tasteful low-saturation tints: sage green / misty blue / cream pink / light khaki / champagne / muted lilac / terracotta / mint / warm beige / pale apricot. You are NOT limited to this palette.
- HARD RULES: background must NEVER be pure white, near-white, pure black or very dark (the software overlays light-coloured text carriers; dark or glaring backgrounds break readability). Prefer warm cream / soft tinted bases.
- State your colour choices explicitly inside style_en (background tint + accent palette, one-line reason tied to the topic).

RICHNESS DEVICES (text-free only; weave AT LEAST ONE into each page's visual direction, vary them across pages, never pile):
- soft rounded colour blocks / abstract geometric shapes as background decor;
- botanical or thematic small illustration ornaments in corners;
- rounded-corner framed inset photo areas (empty frames are fine, content drawn by you);
- dashed arrow chains or connecting lines linking step elements (tutorial pages);
- gentle gradient ribbons or wave bands in light tints;
- small icons-style spot illustrations (check mark, leaf, paw, flask...) drawn as graphics, never as text.

Task: for EACH of the 6 pages write an ENGLISH visual direction (40-80 words) describing ONLY what to depict: concrete subject, environment/props, lighting (direction & quality), camera angle/framing, colour mood (consistent with your colour direction), plus the richness device assigned to that page. The subject MUST be exactly what that page's Chinese copy is about — never replace it with symbols or metaphors. Keep all 6 pages in the SAME unified style and SAME colour direction; only the scene changes per page.

Output STRICT JSON only, no markdown fences, no extra text:
{{"style_en": "<one English paragraph, 50-80 words: unified visual essence INCLUDING the chosen colour direction — background tint, accent palette, lighting, texture, decor>",
 "pages": ["<EN visual direction page 1>", "...", "...", "...", "...", "<page 6>"]}}

【6 pages of Chinese on-image copy】
{pages}"""


async def add_visual_note(note: str, source: str = "review") -> None:
    """把人工反馈沉淀进视觉笔记池（下一次扩写自动带上）。

    fire-and-forget：任何失败只打印，绝不阻塞调用方（审核/修正主流程）。
    """
    note = (note or "").strip()
    if not note:
        return
    try:
        from src.db.session import SessionLocal
        async with SessionLocal() as session:
            await session.execute(
                text("INSERT INTO visual_notes (note, source) VALUES (:n, :s)"),
                {"n": note[:500], "s": source})
            await session.commit()
    except Exception:
        traceback.print_exc()


async def _recent_notes(limit: int = 8) -> list[str]:
    """最近的视觉反馈笔记（新的在前）。"""
    from src.db.session import SessionLocal
    try:
        async with SessionLocal() as session:
            rows = (await session.execute(text(
                "SELECT note FROM visual_notes ORDER BY id DESC LIMIT :n"),
                {"n": limit})).scalars().all()
            return [r for r in rows if r]
    except Exception:
        traceback.print_exc()
        return []


def _parse_visual(text_: str):
    """解析 {"style_en":..., "pages":[6]}；任何不合格返回 None。"""
    try:
        raw = (text_ or "").strip()
        if raw.startswith("```"):
            raw = raw.strip("`").lstrip("json").strip()
        obj = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        style_en = str(obj.get("style_en") or "").strip()
        pages = [str(p).strip() for p in (obj.get("pages") or [])]
        if len(pages) != 6 or not all(pages) or not style_en:
            return None
        return {"style_en": style_en, "pages": pages}
    except Exception:
        return None


async def write_page_visuals(style_name: str, style_desc: str,
                             page_bodies: list, llm_call=None) -> dict | None:
    """6 页中文文案 → {"style_en", "pages":[6 条英文视觉描述]}；失败 None。

    llm_call 可注入（测试/调用方统一 failover 口径）；默认走 DeepSeek 主、
    Kimi 备的 call_with_failover。笔记池自动注入（读库失败不影响主流程）。
    """
    bodies = list(page_bodies or [])[:6]
    while len(bodies) < 6:
        bodies.append("")
    notes = await _recent_notes()
    notes_txt = "\n".join(f"- {n}" for n in notes) if notes else "（none yet）"
    msg = (_VISUAL_PROMPT
           .replace("{style_name}", style_name or "")
           .replace("{style_desc}", style_desc or "")
           .replace("{notes}", notes_txt)
           .replace("{pages}", "\n".join(
               f"Page {i}：{b}" for i, b in enumerate(bodies, 1))))
    try:
        if llm_call is None:
            from src.gateway.failover import call_with_failover
            from src.gateway.failover import DEEPSEEK_MODEL, KIMI_MODEL
            r = await call_with_failover(msg, DEEPSEEK_MODEL, KIMI_MODEL,
                                         max_retries=1)
        else:
            r = await llm_call(msg)
        return _parse_visual(r.get("text") or "")
    except Exception:
        traceback.print_exc()
        return None
