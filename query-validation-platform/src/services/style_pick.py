"""生图视觉风格自适应选择（2026-08-28，迁移 011/012，移植同事 8003 思路）。

asset_gen 生图前为任务选定一种视觉风格（6 张图共用），优先级（迁移 012 用户隔离）：
0. 任务创建者钉了个人默认风格（users.default_style）→ 直接使用，跳过 LLM；
1. 创建者个人库（style_keywords 启用条目）非空 → 个人库中选；
2. 公共库（owner_id IS NULL 启用条目）非空 → 公共库中选；
3. 都空 → 回退内置 8 风格。
选择方式：LLM 按 query+正文摘要选最贴合的一种；LLM 失败/结果不在库中
→ 关键词命中评分兜底（命中最多者胜，并列/全零随机）。
选中风格的描述词替换生图模板中的固定风格句（见 prompt_versions.get_image_prompt）。
"""
import random
import re
import traceback

from sqlalchemy import select, text

from src.db.session import SessionLocal
from src.models.styles import StyleKeyword

# 内置 10 种图片视觉风格库：(风格名, 描述词, 匹配关键词)——描述词移植同事实现，
# 关键词按同事组合导入的风格/垂类思路补配，用于 LLM 失效时的命中评分兜底。
# 2026-09-01 借鉴 8003（依 836 张人工满意样例训练）：新增首选「自然写实暖调」
# （样例主体风格）与「实拍产品渲染」，治愈暖彩/3D渲染/杂志编辑描述词同步细化
IMAGE_STYLE_LIBRARY = [
    ("自然写实暖调", "奶油米/浅米黄低饱和暖底、写实摄影主体、柔和自然光、浅景深、深棕+暖橘双色大标题（关键词暖橘强调）、衬线宋体或黑体、暖棕细分隔线、圆角卡片边框、要点前小圆图标、关键信息暖橘强调、主题色彩色胶囊标签压白字、角落植物叶影或线稿小图标轻装饰、留白约五成，像人工精修的杂志级卡片",
     "教程,生活,美食,校园,教育,科普,育儿,母婴"),
    ("实拍产品渲染", "高精度产品渲染或棚拍质感、柔和影棚光、地面轻微倒影、奶油米浅色背景、深字压浅底配暖橘色强调、参数用细线刻度条标注、尺寸类信息细线标注，真实可信",
     "产品,单品,数码,家电,参数,开箱,评测"),
    ("治愈暖彩", "柔和暖色调、圆润造型、充足留白、治愈呼吸感、高清",
     "情感,治愈,心情,生活,育儿,母婴,宠物"),
    ("真实摄影", "真实实拍质感、自然光影、浅景深、生活化场景、高清细节",
     "测评,实测,实拍,探店,家居,美食,穿搭"),
    ("扁平极简", "扁平矢量插画、大色块几何构图、低饱和配色、极简大量留白",
     "教程,攻略,科普,知识,效率,办公,学习"),
    ("3D渲染", "C4D 三维质感、柔和材质光泽、轻拟物造型、低饱和渐变背景",
     "数码,手机,科技,新品,参数,芯片,智能"),
    ("手绘线稿", "手绘钢笔线稿加水彩淡彩、纸张肌理、自然笔触感",
     "旅行,手绘,文艺,手账,校园,绘本"),
    ("杂志编辑", "时尚杂志编辑排版、大标题、高级灰底色、克制配色、细金线点缀",
     "时尚,美妆,护肤,潮流,轻奢,品牌"),
    ("信息图表", "图标化信息呈现、图表化数据可视化、清晰导视结构",
     "数据,对比,榜单,统计,价格,费用,排行"),
    ("复古印刷", "米色纸底、复古双色调印刷、噪点肌理、旧海报质感",
     "复古,怀旧,历史,老字号,非遗,经典"),
]

# 内置风格变体轴（2026-09-02 反同质化方案 #2）：风格 = 签名层（描述词，固定
# 识别度）+ 变体层（每任务按 task_id 采样一条追加）。变体只动「可轮换维度」
# ——强调色/装饰元素/构图密度，绝不动签名（底色体系、标题双色、字体）。
# DB 条目（个人/公共库）设了 variants 字段时优先用 DB 的，否则回本表
_BUILTIN_VARIANTS = {
    "自然写实暖调": [
        "本篇强调暖色改用砖红（不用暖橘），装饰元素用角落波点",
        "本篇强调暖色改用姜黄（不用暖橘），装饰元素用细线分隔与小圆点",
        "本篇强调暖色改用枫叶橙（不用暖橘），装饰元素用角落干花/叶片剪影",
        "本篇构图偏紧凑（留白约三成），强调暖色保持暖橘",
    ],
    "实拍产品渲染": [
        "本篇强调色改用砖红（不用暖橘），产品角度取正侧面",
        "本篇强调色改用墨蓝（不用暖橘），产品角度取三分之四俯视",
        "本篇地面倒影略明显，背景加一道极浅的水平分色带",
    ],
    "治愈暖彩": [
        "本篇主色偏蜜桃粉，装饰用圆润小云与圆点",
        "本篇主色偏鹅黄，装饰用微笑太阳与小星星线稿",
        "本篇主色偏薄荷绿，装饰用圆叶与小气泡",
    ],
    "真实摄影": [
        "本篇取晨间侧光，色温偏暖",
        "本篇取午后柔光，色温中性",
        "本篇取窗边逆光轮廓，背景虚化更明显",
    ],
    "扁平极简": [
        "本篇主色块用雾蓝系，辅色芥末黄",
        "本篇主色块用鼠尾草绿系，辅色陶土橙",
        "本篇主色块用灰紫系，辅色珊瑚粉",
    ],
    "3D渲染": [
        "本篇材质偏哑光黏土质感，背景渐变取同色系浅淡过渡",
        "本篇材质偏柔光塑料质感，加一处微型展台",
        "本篇材质偏磨砂玻璃质感，背景带轻微景深光斑",
    ],
    "手绘线稿": [
        "本篇水彩淡彩偏青绿调，纸张肌理稍明显",
        "本篇水彩淡彩偏赭石调，线条更松",
        "本篇水彩淡彩偏灰蓝调，加少量留白飞白",
    ],
    "杂志编辑": [
        "本篇细金线改用细银线，灰底偏冷",
        "本篇点缀色改用酒红，灰底偏暖",
        "本篇点缀色改用墨绿，标题区左对齐",
    ],
    "信息图表": [
        "本篇图表色板用蓝绿双色，导视用圆点序号",
        "本篇图表色板用橙灰双色，导视用方形序号",
        "本篇图表色板用紫黄双色，连接线用虚线",
    ],
    "复古印刷": [
        "本篇双色调用蓝黑+米，噪点稍粗",
        "本篇双色调用砖红+米，边框用复古细花边",
        "本篇双色调用墨绿+米，做旧折痕感略明显",
    ],
}


async def variant_for(style_name: str, owner_id=None, seed: int = 0) -> str | None:
    """采样一条风格变体描述（反同质化，2026-09-02）。查找顺序：个人库条目
    variants → 公共库 → 内置变体池；都没有返回 None。seed 通常为 task_id 的
    稳定散列（同一任务永远采到同一条，重出图风格不漂移）。"""
    name = (style_name or "").strip()
    if not name:
        return None
    pool = None
    try:
        async with SessionLocal() as session:
            if owner_id is not None:
                row = (await session.execute(
                    select(StyleKeyword).where(
                        StyleKeyword.owner_id == owner_id,
                        StyleKeyword.style_name == name))).scalars().first()
                if row and row.variants:
                    pool = row.variants
            if pool is None:
                row = (await session.execute(
                    select(StyleKeyword).where(
                        StyleKeyword.owner_id.is_(None),
                        StyleKeyword.style_name == name))).scalars().first()
                if row and row.variants:
                    pool = row.variants
    except Exception:
        traceback.print_exc()
        pool = None
    frags = []
    if pool:
        frags = [f.strip() for f in re.split(r"[；;\n]", pool) if f.strip()]
    if not frags:
        frags = _BUILTIN_VARIANTS.get(name, [])
    if not frags:
        return None
    return "本篇风格变体：" + frags[seed % len(frags)] + "。"


_PICK_PROMPT = """你是小红书图文的视觉总监。根据下面的选题和正文摘要，从给定视觉风格库中选一种最贴合内容气质的图片风格。
只输出风格名本身（必须与库中名称完全一致），不要输出任何其他内容。

【可选风格】
{library}

【选题】
{query}

【正文摘要】
{body}"""


def _keyword_pick(candidates: list, text: str, weights: dict = None) -> tuple:
    """关键词命中评分兜底：命中数 × 偏好权重（学习降级，上限 2.0）最高者胜，
    并列（含全零）随机。"""
    weights = weights or {}
    scored = []
    for name, desc, keywords in candidates:
        hits = sum(1 for kw in re.split(r"[,，]", keywords or "")
                   if kw.strip() and kw.strip() in text)
        scored.append((hits * weights.get(name, 1.0), name, desc))
    best = max(s[0] for s in scored)
    top = [(n, d) for h, n, d in scored if h == best]
    return random.choice(top)


async def _style_candidates(owner_id, public: bool) -> list:
    """指定作用域启用条目 → [(风格名, 描述词, 关键词)]；空库返回 []。
    public=True 查公共库（owner_id IS NULL），否则查该用户的个人库。"""
    scope = StyleKeyword.owner_id.is_(None) if public \
        else StyleKeyword.owner_id == owner_id
    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(StyleKeyword).where(StyleKeyword.enabled, scope)
            .order_by(StyleKeyword.created_at))).scalars().all())
    return [(r.style_name, r.description, r.keywords) for r in rows]


async def _default_style(owner_id) -> str | None:
    """创建者钉的个人默认风格名（users.default_style，迁移 012）；未钉返回 None。"""
    if owner_id is None:
        return None
    async with SessionLocal() as session:
        return (await session.execute(
            text("SELECT default_style FROM users WHERE id = :u"),
            {"u": owner_id})).scalar()


async def _usage_weights(owner_id) -> tuple:
    """近 20 条任务（滑动窗口）的风格使用统计 → ({style: weight}, 窗口任务数)。

    学习降级设计（2026-09-01「不同用户不同风格」方案）：偏好学习只在用户
    **未钉选默认风格**时参与选风格，且只做加权排序、永不替换用户选择——
    权重 = 1 + 使用频次加成 × (0.5 + 通过率)，上限 2.0，避免正反馈收敛
    「越用越窄」；钉了默认的用户走优先级 0 直通，学习完全不影响。
    """
    if owner_id is None:
        return {}, 0
    async with SessionLocal() as session:
        rows = (await session.execute(text(
            "SELECT gen_image_style, status FROM tasks"
            " WHERE created_by = :u AND gen_image_style IS NOT NULL"
            "   AND gen_image_style <> ''"
            " ORDER BY created_at DESC LIMIT 20"), {"u": owner_id})).all()
        # 探索位按全量任务数取模（窗口满 20 后 window_n 恒为 20，取模会每次都
        # 触发探索；全量计数才保证每 5 条任务恰好 1 条探索）
        total = (await session.execute(text(
            "SELECT count(*) FROM tasks"
            " WHERE created_by = :u AND gen_image_style IS NOT NULL"
            "   AND gen_image_style <> ''"), {"u": owner_id})).scalar() or 0
    uses, appr = {}, {}
    for style, status in rows:
        uses[style] = uses.get(style, 0) + 1
        if status == "approved":
            appr[style] = appr.get(style, 0) + 1
    weights = {}
    for style, n in uses.items():
        rate = appr.get(style, 0) / n
        w = 1.0 + min(1.0, 0.15 * n) * (0.5 + rate)
        weights[style] = round(min(2.0, w), 3)
    return weights, total


async def pick_image_style(query: str, body: str, owner_id=None, llm_call=None) -> tuple:
    """选定生图视觉风格，返回 (风格名, 描述词)。

    owner_id：任务创建者（决定个人默认风格与个人库）；llm_call：文本模型调用
    入口（async，签名同 call_with_failover），由调用方注入以便测试 mock；
    缺省走 failover 主备通道。LLM 失败/结果不在库中自动退回关键词命中评分，
    任何情况下都返回库内合法风格。
    """
    # 优先级 0：个人默认风格直通（跳过 LLM 与一切学习加权——用户显式选择永远优先）
    default = await _default_style(owner_id)
    if default:
        return default, await style_desc_for(default, owner_id)
    if llm_call is None:
        from src.gateway.failover import call_with_failover, DEEPSEEK_MODEL, KIMI_MODEL

        async def llm_call(prompt):
            return await call_with_failover(prompt, DEEPSEEK_MODEL, KIMI_MODEL,
                                            max_retries=1)
    # 优先级 1/2/3：个人库 → 公共库 → 内置风格
    candidates = await _style_candidates(owner_id, public=False) if owner_id else []
    if not candidates:
        candidates = await _style_candidates(owner_id, public=True)
    if not candidates:
        candidates = list(IMAGE_STYLE_LIBRARY)
    names = {n for n, _, _ in candidates}
    # 学习降级：未钉选时才用历史偏好加权（上限 2.0）；候选排序把高权重风格
    # 放前面（温和引导 LLM），关键词兜底按 命中数×权重 评分
    weights, task_n = await _usage_weights(owner_id)
    if weights:
        candidates = sorted(
            candidates, key=lambda c: weights.get(c[0], 1.0), reverse=True)
    # 探索位（防收敛）：候选 ≥3 时每 5 条任务有 1 条从权重下半区随机选一种，
    # 保证用户始终能接触到非高频风格、不被锁死在历史上
    if weights and len(candidates) >= 3 and task_n > 0 and task_n % 5 == 0:
        bottom = candidates[len(candidates) // 2:]
        name, desc = random.choice(bottom)[0], None
        desc = next(d for n, d, _ in candidates if n == name)
        return name, desc
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
    return _keyword_pick(candidates, (query or "") + "\n" + (body or "")[:500],
                         weights=weights)


async def style_desc_for(style_name: str, owner_id=None) -> str | None:
    """按风格名反查描述词：个人库（含停用条目，保持已选定风格稳定）→ 公共库
    → 内置库；查不到/出错返回 None（提示词沿用模板默认风格句）。"""
    name = (style_name or "").strip()
    if not name:
        return None
    try:
        async with SessionLocal() as session:
            if owner_id is not None:
                row = (await session.execute(
                    select(StyleKeyword).where(
                        StyleKeyword.owner_id == owner_id,
                        StyleKeyword.style_name == name))).scalars().first()
                if row and row.description:
                    return row.description
            row = (await session.execute(
                select(StyleKeyword).where(
                    StyleKeyword.owner_id.is_(None),
                    StyleKeyword.style_name == name))).scalars().first()
        if row and row.description:
            return row.description
    except Exception:
        traceback.print_exc()
    for n, d, _ in IMAGE_STYLE_LIBRARY:
        if n == name:
            return d
    return None
