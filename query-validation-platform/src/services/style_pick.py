"""生图视觉风格自适应选择（2026-08-28，迁移 011，移植同事 8003 思路）。

asset_gen 生图前为任务选定一种视觉风格（6 张图共用）：
- 用户风格库（style_keywords 启用条目，设置页/CSV 可训练）非空 → 在用户库中选；
- 库空 → 回退内置 8 风格；
- 选择方式：LLM 按 query+正文摘要选最贴合的一种；LLM 失败/结果不在库中
  → 关键词命中评分兜底（命中最多者胜，并列/全零随机）。
选中风格的描述词替换生图模板中的固定风格句（见 prompt_versions.get_image_prompt）。
"""
import random
import re
import traceback

from sqlalchemy import select

from src.db.session import SessionLocal
from src.models.styles import StyleKeyword

# 内置 8 种图片视觉风格库：(风格名, 描述词, 匹配关键词)——描述词移植同事实现，
# 关键词按同事组合导入的风格/垂类思路补配，用于 LLM 失效时的命中评分兜底
IMAGE_STYLE_LIBRARY = [
    ("治愈暖彩", "柔和暖色调插画风、圆润造型、充足留白、治愈呼吸感、高清",
     "情感,治愈,心情,生活,育儿,母婴,宠物"),
    ("真实摄影", "真实实拍质感、自然光影、浅景深、生活化场景、高清细节",
     "测评,实测,实拍,探店,家居,美食,穿搭"),
    ("扁平极简", "扁平矢量插画、大色块几何构图、低饱和配色、极简大量留白",
     "教程,攻略,科普,知识,效率,办公,学习"),
    ("3D渲染", "C4D 三维质感、柔和材质光泽、轻拟物造型、明快渐变背景",
     "数码,手机,科技,新品,参数,芯片,智能"),
    ("手绘线稿", "手绘钢笔线稿加水彩淡彩、纸张肌理、自然笔触感",
     "旅行,手绘,文艺,手账,校园,绘本"),
    ("杂志编辑", "时尚杂志编辑排版、衬线大标题、高级灰底色、克制配色",
     "时尚,美妆,护肤,潮流,轻奢,品牌"),
    ("信息图表", "图标化信息呈现、图表化数据可视化、清晰导视结构",
     "数据,对比,榜单,统计,价格,费用,排行"),
    ("复古印刷", "米色纸底、复古双色调印刷、噪点肌理、旧海报质感",
     "复古,怀旧,历史,老字号,非遗,经典"),
]

_PICK_PROMPT = """你是小红书图文的视觉总监。根据下面的选题和正文摘要，从给定视觉风格库中选一种最贴合内容气质的图片风格。
只输出风格名本身（必须与库中名称完全一致），不要输出任何其他内容。

【可选风格】
{library}

【选题】
{query}

【正文摘要】
{body}"""


def _keyword_pick(candidates: list, text: str) -> tuple:
    """关键词命中评分兜底：命中数最多者胜，并列（含全零）随机。"""
    scored = []
    for name, desc, keywords in candidates:
        hits = sum(1 for kw in re.split(r"[,，]", keywords or "")
                   if kw.strip() and kw.strip() in text)
        scored.append((hits, name, desc))
    best = max(s[0] for s in scored)
    top = [(n, d) for h, n, d in scored if h == best]
    return random.choice(top)


async def _user_style_candidates() -> list:
    """启用中的用户风格库条目 → [(风格名, 描述词, 关键词)]；空库返回 []。"""
    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(StyleKeyword).where(StyleKeyword.enabled)
            .order_by(StyleKeyword.created_at))).scalars().all())
    return [(r.style_name, r.description, r.keywords) for r in rows]


async def pick_image_style(query: str, body: str, llm_call=None) -> tuple:
    """选定生图视觉风格，返回 (风格名, 描述词)。

    llm_call：文本模型调用入口（async，签名同 call_with_failover），由调用方
    注入以便测试 mock；缺省走 failover 主备通道。LLM 失败/结果不在库中自动
    退回关键词命中评分，任何情况下都返回库内合法风格。
    """
    if llm_call is None:
        from src.gateway.failover import call_with_failover, DEEPSEEK_MODEL, KIMI_MODEL

        async def llm_call(prompt):
            return await call_with_failover(prompt, DEEPSEEK_MODEL, KIMI_MODEL,
                                            max_retries=1)
    candidates = await _user_style_candidates()
    if not candidates:
        candidates = list(IMAGE_STYLE_LIBRARY)
    names = {n for n, _, _ in candidates}
    try:
        library = "\n".join(f"- {n}：{d}" for n, d, _ in candidates)
        prompt = (_PICK_PROMPT.replace("{library}", library)
                  .replace("{query}", (query or "").strip())
                  .replace("{body}", (body or "")[:300].strip()))
        r = await llm_call(prompt)
        picked = (r.get("text") or r.get("content") or "").strip().strip("\"'`。.* ")
        if picked in names:
            return picked, next(d for n, d, _ in candidates if n == picked)
    except Exception:
        traceback.print_exc()  # LLM 失败不阻塞出图，走关键词兜底
    return _keyword_pick(candidates, (query or "") + "\n" + (body or "")[:500])


async def style_desc_for(style_name: str) -> str | None:
    """按风格名反查描述词：先查用户库（含停用条目，保持已选定风格稳定），再查
    内置库；查不到/出错返回 None（提示词沿用模板默认风格句）。"""
    name = (style_name or "").strip()
    if not name:
        return None
    try:
        async with SessionLocal() as session:
            row = (await session.execute(
                select(StyleKeyword).where(
                    StyleKeyword.style_name == name))).scalars().first()
        if row and row.description:
            return row.description
    except Exception:
        traceback.print_exc()
    for n, d, _ in IMAGE_STYLE_LIBRARY:
        if n == name:
            return d
    return None
