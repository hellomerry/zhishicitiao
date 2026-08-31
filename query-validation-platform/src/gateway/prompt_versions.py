"""提示词版本库：按 (用途, mode) 提供正文/生图提示词。"""

# 共享风格词（2026-08-26 对齐用户生产环境真实提示词 8.2/8.3：
# 补信息增益/风格锚定封面/文字不重复，字数上限 40→100——分页文案本身 25-50 字，
# 40 字上限会迫使模型删减文案，与「忠于分页文案」冲突；伪汉字禁令保留，系我方更严项。
# 2026-08-28 主体锚定条款：无参考图模式下「坚韧」被 gpt-image-2 与 Gemini 双双
# 具象化成石缝发芽植物（同词条 A/B 对比实测跑题），故硬性要求主体直接描绘主题事物，
# 风格词仅作光影色调氛围；用户生产提示词因总有实景参考图压主体而无此问题）
# 固定风格句（2026-08-28 风格自适应）：任务选定视觉风格后，该句被所选风格的
# 描述词替换（get_image_prompt 的 style_desc 参数）；改文案必须三处同步。
FIXED_IMAGE_STYLE_SENTENCE = "坚韧治愈风、高清、极简高级，背景不太白也不太暗；"
# 主体锚定条款（2026-08-31 动态主体注入）：无参考图模式下让生图模型自己从抽象
# 文案解读主体会漂移（实测冰牛奶做法封面被画成石头堆），故 asset_gen 每页生图前
# 先用 LLM 从分页文案提取具体画面主体，get_image_prompt(page_subject=...) 把本句
# 替换为动态主体句；自定义模板含同样锚定句时也会被替换。改文案必须两处同步
# （_SHARED_IMAGE_STYLE / _SHARED_IMAGE_STYLE_NOTEXT 均引用本常量）。
SUBJECT_ANCHOR_SENTENCE = (
    "画面主体必须直接描绘本页文案所讲的事物本身（例如文案讲冰牛奶饮品，"
    "画面就必须出现牛奶饮品杯），所选风格词仅作光影色调氛围，"
    "严禁把风格词具象化成植物、发芽、石缝、山峰等与主题无关的象征隐喻物。")
_SHARED_IMAGE_STYLE = (
    "竖版3:4图文卡片，一级/二级标题与正文字号对应（至关重要）。"
    "图片与正文内容强相关、具有信息增益：不是简单重复文字，而是对文案的提炼和可视化表达。"
    "主体清晰不被遮挡、展现完整主体不裁剪关键特征。"
    + SUBJECT_ANCHOR_SENTENCE
    + FIXED_IMAGE_STYLE_SENTENCE +
    "全套图片风格与封面保持一致。"
    "所有文字必须使用标准可读中文黑体，禁止艺术化变形、阴影、描边、透视扭曲，"
    "正文统一基线对齐、可印刷级清晰；图中文字不超过100字，不出现字号过小的文字。"
    # 文字颜色/底衬（2026-08-31 实测：只写「禁止深色框」无效——模型为保证白字
    # 对比度默认深底白字，负向措辞被反向注意；必须正向给出深色字+浅色底的搭配）
    "文字默认使用深灰色或黑色，直接排版在画面的浅色留白区域上，不要底色、不要背景框；"
    "个别页如确需底衬保证可读性，只能用米白或浅奶油色的浅色半透明底，"
    "全套6页中深色底文字框最多出现1次，严禁每页都用深色矩形框压白色文字。"
    "图中所有汉字必须是中国大陆规范简体字形，严禁日文新字体（如実・対・変・単・図・芸）、"
    "繁体字、异体字、自造字，严禁生成不存在的伪汉字或乱码字符；"
    "把图中文字当作需要逐字精确复制的排版内容，而不是装饰性视觉纹理："
    "给定文案一字不差地呈现，不增字、不漏字、不改写，"
    "拿不准如何正确书写的文字宁可不出现在图中；"
    "不要出现「封面/第X页」等字样。排版不要模板化（每页排版不同），"
    "图片元素不与前页重复，各页图中的文字内容也不重复。"
    "不出现人脸、书籍等元素，尽量不出现带文字的物体。"
)

DRAFT_PROMPTS = {
    "general": "请你以小红书博主的写作风格及模式，结合权威可靠信源的数据库，创作一篇图文内容。要求：简洁清晰、结构完整、总分总结构、每段加小标题、400-700字、无绝对化表述、无emoji、中文标点。",
    "single": "请你以小红书博主的写作风格，结合权威可靠信源的数据库，创作一篇单品深度测评图文。围绕单一产品/事物展开，依次讲透：它是什么、原理或关键参数、实测体验、优点、局限、安全/使用提醒、适合谁。要求：简洁清晰、总分总结构、每段加小标题、400-700字、无绝对化表述、无emoji、中文标点，事实数据需有信源支撑。",
    "compare": "请你以小红书博主的写作风格，结合权威可靠信源的数据库，创作一篇对比类图文。客观对比两个主体（产品/学校/方案等），平分笔墨，逐维度列出各自的事实参数、优劣与适用场景，最后给出取舍建议。要求：简洁清晰、总分总结构、每段加小标题、400-700字、无绝对化表述、无emoji、中文标点，事实数据需有信源支撑，不偏袒任何一方。",
}

# 分页排版轮换指令：同一套风格词下，6 页的构图/布局必须错开，
# 否则 gpt-image 会把每页都画成同一个模板（2026-08-20 用户反馈「每张图重复套用模版」）
_PAGE_LAYOUTS = [
    "本页是封面页：主视觉大图占画面约三分之二，大标题置顶部，副标题只一行，整体留白充足。",
    "本页是要点页：上文下图布局，正文拆成2-3个短句要点纵向排列，用细线或小色块分隔。",
    "本页是特写页：主体特写充满画面，文字只放在底部约四分之一的横条区域内。",
    "本页是清单页：卡片式分栏布局，信息分成2-4块排列，每块一个小标题，块间留明显间距。",
    "本页是场景页：全幅场景插画铺满画面，文字只置于顶部留白或浅色区域内。",
    "本页是总结页：居中大字结论，下方最多两行小字，视觉收尾干净利落。",
]

# 文字呈现形式分页轮换（2026-08-31 用户反馈「每页都是深色矩形框压字」）：
# 提示词原来只约束字体、没约束文字载体形式，模型默认每页深色圆角矩形框。
# 与 _PAGE_LAYOUTS 同步按页码追加（仅 no_text=False 的有字版），错开 6 页文字形式
_TEXT_PRESENTATIONS = [
    "本页文字呈现形式：深灰色标题与正文直接排版在纯色留白区域，不使用任何底框或底色块。",
    "本页文字呈现形式：杂志式排版，深灰色大标题下方用一条细分隔线引出正文，全程无底色块。",
    "本页文字呈现形式：关键短句用浅色（米白/浅奶油色，非深色）胶囊形圆角标签呈现，标签内深色文字，标签间留明显间距。",
    "本页文字呈现形式：左侧约四分之一为竖排深色大标题区，其余文字在右侧留白区横向排版，无底框。",
    "本页文字呈现形式：深色文字置于底部浅色（米白/浅奶油色，非深色）半透明横条上，横条通透、不遮挡画面主体。",
    "本页文字呈现形式：居中无框深色大标题加充足留白，小字紧随其后，全页不出现任何底框。",
]

IMAGE_PROMPTS = {
    "general": "通用科普/教程配图，纯 AI 生成、无参考图。" + _SHARED_IMAGE_STYLE + "【本页必须出现在图中的文字，逐字准确呈现】：{page_body}",
    "single": "单品评测配图，将提供的参考实景图融入画面：去水印、去人物、实景图不重复、每页实景图不宜过多以免杂乱；不删减参考图上的文字，也不额外添加其他图片。" + _SHARED_IMAGE_STYLE + "【本页必须出现在图中的文字，逐字准确呈现】：{page_body}",
    "compare": "对比类配图。硬性要求：每页必须在同一画面中同时呈现两个主体做对比（左右分栏或上下对比构图，参考图顺序不能乱：主体A的参考图在前、主体B的在后），展示同一维度下两者的差异；不同页聚焦不同角度（整体外观、正面、侧面、局部细节、使用场景）。将两个主体的参考实景图融入画面：去水印、去人物、实景图不重复。" + _SHARED_IMAGE_STYLE + "【本页必须出现在图中的文字，逐字准确呈现】：{page_body}",
}

# ===== 无字版生图提示词（文字后期合成模式，2026-08-27）=====
# AI 只画背景、文字由 text_composite 用真实字体合成：提示词必须严禁图中出现任何
# 文字，并要求在指定区域预留干净留白。文字区位置与 text_composite._ZONE_BY_PAGE
# 一一对应，改动时必须两边同步。自定义模板（提示词库）都含文字要求、与此模式
# 冲突，故本模式固定使用内置模板。
_MODE_PREFIX_NOTEXT = {
    "general": "通用科普/教程配图插画，纯 AI 生成、无参考图。",
    "single": "单品评测配图插画，将提供的参考实景图融入画面：去水印、去人物、去掉参考图上的一切文字、实景图不重复、每页实景图不宜过多以免杂乱；不额外添加其他图片。",
    "compare": "对比类配图插画。硬性要求：每页必须在同一画面中同时呈现两个主体做对比（左右分栏或上下对比构图，参考图顺序不能乱：主体A的参考图在前、主体B的在后），展示同一维度下两者的差异；不同页聚焦不同角度（整体外观、正面、侧面、局部细节、使用场景）。将两个主体的参考实景图融入画面：去水印、去人物、去掉参考图上的一切文字、实景图不重复。",
}

_SHARED_IMAGE_STYLE_NOTEXT = (
    "竖版3:4图文卡片插画。图片与主题强相关、具有信息增益：对内容的提炼和可视化表达。"
    "主体清晰不被遮挡、展现完整主体不裁剪关键特征。"
    + SUBJECT_ANCHOR_SENTENCE
    + FIXED_IMAGE_STYLE_SENTENCE +
    "全套图片风格保持一致。"
    "【绝对禁止】画面中不要出现任何文字：汉字、字母、数字、标点、招牌字、标签字、"
    "屏幕字一律不要（文字将由后期程序精确合成）；不出现人脸、书籍等元素。"
    "图片元素不与前页重复。"
)

# 无字版分页排版轮换：指定本页预留文字区的位置（与 _ZONE_BY_PAGE 对应）
_PAGE_LAYOUTS_NOTEXT = [
    "本页是封面页：主视觉大图占画面上部约三分之二，底部约三分之一保持干净、简洁、低细节，用于后期叠加文字。",
    "本页是要点页：上部约五分之二保持干净、简洁、低细节，用于后期叠加文字，下部为插画主体。",
    "本页是特写页：主体特写充满画面，底部约四分之一保持干净的横条区域，用于后期叠加文字。",
    "本页是清单页：上部约三分之一保持干净、简洁、低细节，用于后期叠加文字，下部信息图形化排列。",
    "本页是场景页：全幅场景插画，顶部约三分之一保持干净、简洁、低细节，用于后期叠加文字。",
    "本页是总结页：画面中部约五分之二保持干净、简洁、低细节，用于后期叠加文字，四周环绕装饰元素。",
]

# 旧版提示词（保留兼容：get_prompt 仍可读 draft_v1 / page_split_v1 / evidence_v1）
PROMPT_VERSIONS = {
    "draft_v1": DRAFT_PROMPTS["general"],
    "page_split_v1": "对文章进行精简和拆分，总文字严格控制到350字以内，包括封面和每一页的文字内容，适合放在图上，每个部分一段话。",
    "evidence_v1": "提取这段话中可验证的事实点（数值、单位、年份、定义、引用、因果），每个事实点标注风险等级。",
}


def get_prompt(name: str, version: str = None) -> str:
    if version:
        key = f"{name}_{version}"
        if key in PROMPT_VERSIONS:
            return PROMPT_VERSIONS[key]
    return PROMPT_VERSIONS[f"{name}_v1"]


def get_draft_prompt(mode: str) -> str:
    return DRAFT_PROMPTS.get(mode, DRAFT_PROMPTS["general"])


def _apply_style_desc(prompt: str, style_desc: str = None) -> str:
    """风格自适应注入（2026-08-28，迁移 011）：任务已选定视觉风格时，把模板里的
    固定风格句替换为该风格的描述词；模板不含固定句（自定义模板）则不强行注入。"""
    desc = (style_desc or "").strip()
    if desc and FIXED_IMAGE_STYLE_SENTENCE in prompt:
        prompt = prompt.replace(FIXED_IMAGE_STYLE_SENTENCE, desc.rstrip("；;") + "；")
    return prompt


def _apply_page_subject(prompt: str, page_subject: str = None) -> str:
    """动态主体锚定（2026-08-31）：asset_gen 已从本页文案提取画面主体时，把
    锚定条款里的通用例子句替换为该主体句；模板不含锚定句（如无主体约束的
    自定义模板）则不强行注入。"""
    subject = (page_subject or "").strip()
    if subject and SUBJECT_ANCHOR_SENTENCE in prompt:
        prompt = prompt.replace(
            SUBJECT_ANCHOR_SENTENCE,
            f"本页画面主体必须是：{subject}，占据画面视觉中心；"
            f"所选风格词仅作光影色调氛围，"
            f"严禁用与本页文案无关的象征隐喻物替代主体。")
    return prompt


def get_image_prompt(mode: str, page_body: str, page_index: int = None,
                     template: str = None, no_text: bool = False,
                     style_desc: str = None, page_subject: str = None) -> str:
    if no_text:
        # 文字后期合成模式：AI 只画无字背景并预留文字区（固定内置模板，
        # 自定义模板都含文字要求、与此模式冲突）
        prompt = (_MODE_PREFIX_NOTEXT.get(mode, _MODE_PREFIX_NOTEXT["general"])
                  + _SHARED_IMAGE_STYLE_NOTEXT)
        if page_index:
            prompt += _PAGE_LAYOUTS_NOTEXT[(page_index - 1) % len(_PAGE_LAYOUTS_NOTEXT)]
        prompt = _apply_style_desc(prompt, style_desc)
        return _apply_page_subject(prompt, page_subject)
    # template：用户自定义生图提示词（替代系统模板），排版轮换仍由代码追加
    template = template or IMAGE_PROMPTS.get(mode, IMAGE_PROMPTS["general"])
    prompt = template.replace("{page_body}", page_body)
    if page_index:
        # 追加本页专属排版指令 + 文字呈现形式，让 6 页构图与文字载体都错开
        prompt += _PAGE_LAYOUTS[(page_index - 1) % len(_PAGE_LAYOUTS)]
        prompt += _TEXT_PRESENTATIONS[(page_index - 1) % len(_TEXT_PRESENTATIONS)]
    prompt = _apply_style_desc(prompt, style_desc)
    return _apply_page_subject(prompt, page_subject)


# 分页文案：由 LLM 把整篇正文改写成 6 页图上文案（替代旧的机械切割，2026-08-20）
PAGES_PROMPT = """你是小红书图文编辑。把下面的文章改写成 6 页图上文案，用于竖版图文卡片。
要求：
1. 输出严格的 JSON 数组，恰好 6 个字符串，不要输出任何其他文字、解释或 markdown 代码围栏。
2. 第 1 页是封面：主标题 + 一句钩子（12-20 字）。
3. 第 2-5 页每页只讲一个核心要点：小标题 + 1-2 句干货（25-50 字），忠于原文的事实与数据，不得编造。
4. 第 6 页是结尾：一句总结 + 适合谁/行动建议（20-40 字）。
5. 全部纯文本：不用 markdown 符号（#、*、- 等），不用 emoji，无绝对化表述，中文标点。
6. 文字必须精简：图上排版空间有限，每页只保留最关键的信息点，宁可少说不可啰嗦。
7. 每页文字都要语句完整通顺，能直接排版在图片上。
8. 严禁输出「标题」「副标题」「正文」这类字面占位词：直接写真实文案内容本身，
   不要把结构名称当成文案写出来（真实教训：封面曾被渲染成"标题"两个大字）。

文章：
{body}"""


def get_pages_prompt(body: str) -> str:
    return PAGES_PROMPT.replace("{body}", body)


# 定点重生成：单页文案重写（驳回标记驱动，2026-08-21）
PAGE_REGEN_PROMPT = """你是小红书图文编辑。下面是一篇图文的完整正文，以及其中第 {page_index} 页的原图上文案。
该页在人工审核中被驳回，审核意见如下：
{feedback}

请参考正文，重写第 {page_index} 页的图上文案。
要求：
1. 逐条解决审核意见中的问题，不得再出现同类问题。
2. 忠于正文的事实与数据，不得编造；纯文本，不用 markdown 符号和 emoji，中文标点。
3. 25-50 字，语句完整通顺，能直接排版在图片上。
4. 只输出该页文案本身，不要输出页码、解释或任何其他文字。

正文：
{body}

原第 {page_index} 页文案：
{old_copy}"""


# ============ 提示词库（系统默认 + 用户自定义，2026-08-20） ============
# 系统默认提示词就是本文件里的常量；用户自定义提示词存 prompt_templates 表。
# 流水线解析顺序：任务创建者在该 (stage, mode) 有「启用」的自定义提示词 → 用之，
# 否则回退系统默认。

STAGE_CATALOG = [
    {"stage": "draft_gen", "label": "正文生成",
     "modes": ["general", "single", "compare"],
     "hint": "任务 query 会追加在提示词之后。"},
    {"stage": "page_split", "label": "分页文案",
     "modes": [None],
     "hint": "用 {body} 引用整篇正文；要求模型输出 6 个字符串的 JSON 数组。"},
    {"stage": "image_gen", "label": "配图生成",
     "modes": ["general", "single", "compare"],
     "hint": "用 {page_body} 引用本页文案；分页排版指令由系统自动追加。"},
    {"stage": "page_regen", "label": "单页重写（定点重生成）",
     "modes": [None],
     "hint": "驳回标记驱动。用 {body} 引用正文、{old_copy} 引用原文案、"
             "{feedback} 引用审核意见、{page_index} 引用页码。"},
]


def default_prompt(stage: str, mode: str = None) -> str:
    """系统默认提示词（提示词库里展示的「系统内置」内容）。"""
    if stage == "draft_gen":
        return DRAFT_PROMPTS.get(mode, DRAFT_PROMPTS["general"])
    if stage == "page_split":
        return PAGES_PROMPT
    if stage == "image_gen":
        return IMAGE_PROMPTS.get(mode, IMAGE_PROMPTS["general"])
    if stage == "page_regen":
        return PAGE_REGEN_PROMPT
    raise KeyError(f"unknown prompt stage: {stage}")


async def system_prompt(stage: str, mode: str = None) -> tuple:
    """当前生效的系统默认提示词：admin 落库的覆盖（owner_id IS NULL）优先，
    否则代码内置默认。返回 (content, customized)。"""
    from sqlalchemy import text as _text
    from src.db.session import SessionLocal
    async with SessionLocal() as session:
        r = await session.execute(_text(
            "SELECT content FROM prompt_templates "
            "WHERE stage = :s AND mode IS NOT DISTINCT FROM :m AND owner_id IS NULL "
            "ORDER BY updated_at DESC LIMIT 1"),
            {"s": stage, "m": mode})
        override = r.scalar()
    if override is not None:
        return override, True
    return default_prompt(stage, mode), False


async def get_effective_prompt(stage: str, mode: str, owner_id) -> str:
    """流水线取词入口：创建者启用的自定义 → admin 的系统覆盖 → 代码内置默认。"""
    if owner_id is not None:
        from sqlalchemy import text as _text
        from src.db.session import SessionLocal
        async with SessionLocal() as session:
            r = await session.execute(_text(
                "SELECT content FROM prompt_templates "
                "WHERE stage = :s AND mode IS NOT DISTINCT FROM :m "
                "AND owner_id = :o AND is_active "
                "ORDER BY updated_at DESC LIMIT 1"),
                {"s": stage, "m": mode, "o": str(owner_id)})
            custom = r.scalar()
            if custom:
                return custom
    content, _ = await system_prompt(stage, mode)
    return content
