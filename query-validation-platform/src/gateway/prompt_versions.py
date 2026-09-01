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
    # 底座铁律（2026-09-01 借鉴 8003 风格底座重构，回应「黑框压字/质量欠缺」反馈）：
    # 底色带色相、图文融合、单页单点、质感精致——跨风格通用，与所选风格词无关
    "整卡背景与边框不得纯白或纯黑，按本篇风格主色调呈现带色相的底色，边缘干净利落；"
    "文字区与画面融合自然、过渡柔和，忌生硬矩形拼贴，视觉有呼吸感；"
    "每页只突出一个核心信息点；"
    "主体质感必须精致干净，忌廉价塑料感、忌粗糙未完成的笔触。"
    "所有文字必须使用标准可读中文黑体，禁止艺术化变形、阴影、描边、透视扭曲，"
    "正文统一基线对齐、可印刷级清晰；图中文字不超过100字，不出现字号过小的文字。"
    # 2026-09-01 依据人工满意样例：封面/要点页主标题稳定为双色大标题
    "主标题采用双色排版：主体字为深色，其中1-2个关键词用强调暖色（暖橘/砖红）呈现，"
    "对比类标题的「VS」或对比词也用强调色；"
    # 文字颜色/底衬（2026-08-31 实测：只写「禁止深色框」无效——模型为保证白字
    # 对比度默认深底白字，负向措辞被反向注意；必须正向给出深色字+浅色底的搭配）
    "文字默认使用深灰色或黑色，直接排版在画面的浅色留白区域上，不要底色、不要背景框；"
    "个别页如确需底衬保证可读性，可用米白或浅奶油色的浅色半透明底；"
    # 2026-09-01 依据人工满意样例（图文每日做题 400+ 主题）修正：主题色彩色胶囊
    # 是样例中被采纳的高频形式，此前的深色框禁令把彩色标签也误伤了，予以豁免
    "也可用主题色（暖橘/橄榄绿/灰蓝等饱和彩色，非深黑深灰）的彩色胶囊/圆角标签"
    "压白色文字做重点标注——彩色标签是推荐形式、不算深色框；"
    "全套6页中深色（近黑/深灰）底文字框最多出现1次，严禁每页都用深色矩形框压白色文字。"
    "图中所有汉字必须是中国大陆规范简体字形，严禁日文新字体（如実・対・変・単・図・芸）、"
    "繁体字、异体字、自造字，严禁生成不存在的伪汉字或乱码字符；"
    "把图中文字当作需要逐字精确复制的排版内容，而不是装饰性视觉纹理："
    "给定文案一字不差地呈现，不增字、不漏字、不改写，"
    "拿不准如何正确书写的文字宁可不出现在图中；"
    "不要出现「封面/第X页」等字样。排版不要模板化（每页排版不同），"
    "图片元素不与前页重复，各页图中的文字内容也不重复。"
    "不出现人脸、书籍等元素，尽量不出现带文字的物体。"
)

# 正文创作共享要求（2026-09-01 对齐用户人工生产流程 20260810 第①步豆包创作
# 提示词）：人设真人感、标题公式、字数400-600、细节禁令（开头不用「作为」/
# 禁感叹号波浪号/非必要无双引号）、免责声明与安全警告全部吸收。与标杆交付
# 规范（bench_{mode}，在 draft_gen 节点追加注入）分工：bench 管结构规律与
# 参数密度，本模板管人设口吻、标题写法与合规底线。
_DRAFT_SHARED = (
    "【人设与真人感】以一个合理的第一人称人设写作（亲历者/过来人/真实使用者均可），"
    "开头用自然导语交代背景动机（如「最近不少家长在纠结怎么选，我前后跑了几家」），"
    "避免生硬直接切入知识点；但表达务实克制，就正常人说话的语气，"
    "不喊「家人们」，不用「绝绝子」「天花板」等夸张网络用语。"
    "【标题】大标题不超过25字，公式：主需关键词＋核心看点/卖点＋信息钩子"
    "（让读者一眼知道点进去能得到什么），强需求导向；"
    "标题及内文不用 emoji、波浪号、感叹号，非必要不用双引号。"
    "【结构】总分总结构，除首尾段外每段加一个简短小标题；核心信息前置，"
    "第一段就点题（不要用「作为」开头）；段落间注意换段空行，排版舒朗。"
    "【字数】全文严格控制在400-600字。"
    "【合规】严禁绝对化、夸张化表述，用词准确，不能有事实性差错，数据需有信源支撑；"
    "涉及财产、功效成分、健康等易引发合规风险的信息，结尾统一加免责声明"
    "（如「本文内容仅供参考，不构成专业建议」）；"
    "有明确人身安全隐患的高危操作，额外加明确的安全警告。统一使用中文标点。")

DRAFT_PROMPTS = {
    "general": "请你以小红书博主的写作风格，结合权威可靠信源，创作一篇科普/教程类图文。" + _DRAFT_SHARED,
    "single": "请你以小红书博主的写作风格，结合权威可靠信源，创作一篇单品深度测评图文：围绕单一产品/事物依次讲透它是什么、原理或关键参数、实测体验、优点、局限、安全/使用提醒、适合谁。" + _DRAFT_SHARED,
    "compare": "请你以小红书博主的写作风格，结合权威可靠信源，创作一篇对比类图文：客观对比两个主体（产品/学校/方案等），平分笔墨，逐维度列出各自的事实参数、优劣与适用场景，最后按人群/需求给出取舍建议，不偏袒任何一方。" + _DRAFT_SHARED,
}

# 分页排版轮换指令：同一套风格词下，6 页的构图/布局必须错开，
# 否则 gpt-image 会把每页都画成同一个模板（2026-08-20 用户反馈「每张图重复套用模版」）
_PAGE_LAYOUTS = [
    "本页是封面页：主视觉大图占画面约三分之二，大标题置顶部，副标题只一行，整体留白充足。",
    "本页是要点页：上文下图布局，正文拆成2-3个短句要点纵向排列，每条要点前可配一枚统一的小圆图标，用细线或小色块分隔。",
    "本页是特写页：主体特写充满画面，文字只放在底部约四分之一的横条区域内。",
    "本页是清单/流程页：圆角卡片式分栏布局，信息分成2-4块排列，每块一个小标题，块间留明显间距；若本页内容是步骤/流程，改用编号胶囊（01/02/03）加箭头串联的步骤卡片呈现。",
    "本页是场景页：全幅场景插画铺满画面，文字只置于顶部留白或浅色区域内，图与文字以弧线或斜线自然衔接。",
    "本页是总结页：居中大字结论，下方最多两行小字，视觉收尾干净利落。",
]

# 文字呈现形式分页轮换（2026-08-31 用户反馈「每页都是深色矩形框压字」）：
# 提示词原来只约束字体、没约束文字载体形式，模型默认每页深色圆角矩形框。
# 与 _PAGE_LAYOUTS 同步按页码追加（仅 no_text=False 的有字版），错开 6 页文字形式。
# 2026-09-01 依据人工满意样例（图文每日做题 400+ 主题）扩充：主题色彩色胶囊标签、
# 图标+半透明衬底要点、白/米白圆角横条总结、锯齿/气泡贴纸强调——样例中高频被
# 采纳的形式；分页固定 6 页，本列表必须保持 6 条，新形式并入现有轮换而非追加
_TEXT_PRESENTATIONS = [
    "本页文字呈现形式：深灰色标题与正文直接排版在纯色留白区域，不使用任何底框或底色块；主标题采用双色排版，主体字为深色，其中1-2个关键词用强调暖色（暖橘/砖红）呈现。",
    "本页文字呈现形式：杂志式排版，深灰色大标题下方用一条细分隔线引出正文，可配中英文小字标注，全程无底色块。",
    "本页文字呈现形式：关键短句用主题色（暖橘/橄榄绿/灰蓝等饱和彩色，非深黑深灰）胶囊形圆角标签呈现，标签内白色文字，标签间留明显间距。",
    "本页文字呈现形式：每条要点为一枚统一的小圆图标（对勾/定位/叶片等）加深色文字，底下垫浅色半透明圆角衬底，衬底通透、不遮挡画面主体。",
    "本页文字呈现形式：底部一条白色或米白色圆角横条做总结区，横条内文字用主题强调色（暖橘等），可配一枚小图标，横条不遮挡画面主体。",
    "本页文字呈现形式：核心强调短句用锯齿圆贴或对话气泡贴纸呈现（贴纸为浅色底、深色字），其余文字居中无框、留白充足。",
]

# 通用模式前缀两版（2026-09-01 通用启用实景图）：任务有 official 实图（搜索/
# 手动上传/素材库复用）时 asset_gen 传 has_refs=True，把「纯 AI 生成、无参考图」
# 替换为实景图融入条款；无实图回退纯 AI。自定义模板不含该句时不强行注入
# （与 style_desc/page_subject 注入同一模式）。有字/无字两版文案须同步修改。
GENERAL_NOREF_PREFIX = "通用科普/教程配图，纯 AI 生成、无参考图。"
GENERAL_REF_PREFIX = (
    "通用科普/教程配图，将提供的参考实景图融入画面：去水印、去人物、"
    "实景图不重复、每页实景图不宜过多以免杂乱；不删减参考图上的文字，"
    "也不额外添加其他图片。")
GENERAL_NOREF_PREFIX_NOTEXT = "通用科普/教程配图插画，纯 AI 生成、无参考图。"
GENERAL_REF_PREFIX_NOTEXT = (
    "通用科普/教程配图插画，将提供的参考实景图融入画面：去水印、去人物、"
    "去掉参考图上的一切文字、实景图不重复、每页实景图不宜过多以免杂乱；"
    "不额外添加其他图片。")

IMAGE_PROMPTS = {
    "general": GENERAL_NOREF_PREFIX + _SHARED_IMAGE_STYLE + "【本页必须出现在图中的文字，逐字准确呈现】：{page_body}",
    "single": "单品评测配图，将提供的参考实景图融入画面：去水印、去人物、实景图不重复、每页实景图不宜过多以免杂乱；不删减参考图上的文字，也不额外添加其他图片。" + _SHARED_IMAGE_STYLE + "【本页必须出现在图中的文字，逐字准确呈现】：{page_body}",
    "compare": "对比类配图。硬性要求：每页必须在同一画面中同时呈现两个主体做对比（左右分栏或上下对比构图，参考图顺序不能乱：主体A的参考图在前、主体B的在后），展示同一维度下两者的差异；不同页聚焦不同角度（整体外观、正面、侧面、局部细节、使用场景）。将两个主体的参考实景图融入画面：去水印、去人物、实景图不重复。" + _SHARED_IMAGE_STYLE + "【本页必须出现在图中的文字，逐字准确呈现】：{page_body}",
}

# ===== 无字版生图提示词（文字后期合成模式，2026-08-27）=====
# AI 只画背景、文字由 text_composite 用真实字体合成：提示词必须严禁图中出现任何
# 文字，并要求在指定区域预留干净留白。文字区位置与 text_composite._ZONE_BY_PAGE
# 一一对应，改动时必须两边同步。自定义模板（提示词库）都含文字要求、与此模式
# 冲突，故本模式固定使用内置模板。
_MODE_PREFIX_NOTEXT = {
    "general": GENERAL_NOREF_PREFIX_NOTEXT,
    "single": "单品评测配图插画，将提供的参考实景图融入画面：去水印、去人物、去掉参考图上的一切文字、实景图不重复、每页实景图不宜过多以免杂乱；不额外添加其他图片。",
    "compare": "对比类配图插画。硬性要求：每页必须在同一画面中同时呈现两个主体做对比（左右分栏或上下对比构图，参考图顺序不能乱：主体A的参考图在前、主体B的在后），展示同一维度下两者的差异；不同页聚焦不同角度（整体外观、正面、侧面、局部细节、使用场景）。将两个主体的参考实景图融入画面：去水印、去人物、去掉参考图上的一切文字、实景图不重复。",
}

_SHARED_IMAGE_STYLE_NOTEXT = (
    "竖版3:4图文卡片插画。图片与主题强相关、具有信息增益：对内容的提炼和可视化表达。"
    "主体清晰不被遮挡、展现完整主体不裁剪关键特征。"
    + SUBJECT_ANCHOR_SENTENCE
    + FIXED_IMAGE_STYLE_SENTENCE +
    "全套图片风格保持一致。"
    # 底座铁律（2026-09-01 借鉴 8003）：底色带色相、质感精致，与有字版口径一致
    "整卡背景不得纯白或纯黑，按本篇风格主色调呈现带色相的底色；"
    "画面质感必须精致干净，忌廉价塑料感、忌粗糙未完成的笔触。"
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


def _apply_general_refs(prompt: str, has_refs: bool, no_text: bool) -> str:
    """通用模式有实图时把无参考图前缀替换为实景图融入条款（2026-09-01）。
    其它 mode 模板本就含参考图措辞无需处理；自定义模板不含该句时不强行注入。"""
    if not has_refs:
        return prompt
    noref = GENERAL_NOREF_PREFIX_NOTEXT if no_text else GENERAL_NOREF_PREFIX
    ref = GENERAL_REF_PREFIX_NOTEXT if no_text else GENERAL_REF_PREFIX
    if noref in prompt:
        prompt = prompt.replace(noref, ref)
    return prompt


def get_image_prompt(mode: str, page_body: str, page_index: int = None,
                     template: str = None, no_text: bool = False,
                     style_desc: str = None, page_subject: str = None,
                     has_refs: bool = False) -> str:
    if no_text:
        # 文字后期合成模式：AI 只画无字背景并预留文字区（固定内置模板，
        # 自定义模板都含文字要求、与此模式冲突）
        prompt = (_MODE_PREFIX_NOTEXT.get(mode, _MODE_PREFIX_NOTEXT["general"])
                  + _SHARED_IMAGE_STYLE_NOTEXT)
        if page_index:
            prompt += _PAGE_LAYOUTS_NOTEXT[(page_index - 1) % len(_PAGE_LAYOUTS_NOTEXT)]
        prompt = _apply_style_desc(prompt, style_desc)
        prompt = _apply_page_subject(prompt, page_subject)
        return _apply_general_refs(prompt, has_refs, True)
    # template：用户自定义生图提示词（替代系统模板），排版轮换仍由代码追加
    template = template or IMAGE_PROMPTS.get(mode, IMAGE_PROMPTS["general"])
    prompt = template.replace("{page_body}", page_body)
    if page_index:
        # 追加本页专属排版指令 + 文字呈现形式，让 6 页构图与文字载体都错开
        prompt += _PAGE_LAYOUTS[(page_index - 1) % len(_PAGE_LAYOUTS)]
        prompt += _TEXT_PRESENTATIONS[(page_index - 1) % len(_TEXT_PRESENTATIONS)]
    prompt = _apply_style_desc(prompt, style_desc)
    prompt = _apply_page_subject(prompt, page_subject)
    return _apply_general_refs(prompt, has_refs, False)


# 分页文案：由 LLM 把整篇正文改写成 6 页图上文案（替代旧的机械切割，2026-08-20）
# 2026-09-01 对齐用户人工流程第⑦步：小标题保持原文不精简不改写、6 页合计 ≤350 字
PAGES_PROMPT = """你是小红书图文编辑。把下面的文章改写成 6 页图上文案，用于竖版图文卡片。
要求：
1. 输出严格的 JSON 数组，恰好 6 个字符串，不要输出任何其他文字、解释或 markdown 代码围栏。
2. 第 1 页是封面：主标题 + 一句钩子（12-20 字）。
3. 第 2-5 页每页只讲一个核心要点：小标题 + 1-2 句干货（25-50 字），忠于原文的事实与数据，不得编造。
4. 第 6 页是结尾：一句总结 + 适合谁/行动建议（20-40 字）。
5. 正文里的小标题保持原文，不精简、不改写；6 页合计总文字控制在 350 字以内。
6. 六页文字量基本均衡（2026-09-01 借鉴 8003）：动笔前先规划每页字数骨架，
   第 2-5 页之间任意两页字数相差不超过 25 字，严禁某页只有十几个字而另一页接近上限。
7. 全部纯文本：不用 markdown 符号（#、*、- 等），不用 emoji，无绝对化表述，中文标点。
8. 文字必须精简：图上排版空间有限，每页只保留最关键的信息点，宁可少说不可啰嗦。
9. 每页文字都要语句完整通顺，能直接排版在图片上。
10. 严禁输出「标题」「副标题」「正文」这类字面占位词：直接写真实文案内容本身，
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
3. 25-50 字，语句完整通顺，能直接排版在图片上，与该篇其他要点页文字量
   基本均衡（相差不超过 25 字）。
4. 只输出该页文案本身，不要输出页码、解释或任何其他文字。

正文：
{body}

原第 {page_index} 页文案：
{old_copy}"""


# 校稿润色（2026-09-01 对齐人工流程两轮校稿：一轮删夸大表述与未证实信息、
# 二轮削 AI 腔提真人感）。在 draft_gen 之后、rule_check 之前执行；
# LLM 失败或输出过短时节点不阻塞，沿用 draft_gen 原稿。
DRAFT_POLISH_PROMPT = """你是资深内容校稿编辑。请对下面的稿件做一轮校稿润色，只输出校稿后的最终全文，不要输出任何解释。
要求：
1. 事实核查：对没有把握的具体数字、年份、名称，改为约数表述（如「约」「近」）或直接删去，不得保留存疑的精确表述；严禁绝对化、夸大化用词。
2. 真人感：开头导语自然、有亲历感；删掉生硬AI腔（如「总而言之」「综上所述」「值得注意的是」等套话），语气务实克制，像正常人在说话。
3. 标题：大标题不超过25字，保留主需关键词和看点钩子；不用emoji、感叹号、波浪号。
4. 小标题：保持原文的小标题，不新增、不替换；个别明显冗长的可精简至8字以内。
5. 字数：全文控制在700字以内，超出则删减次要信息，保留核心干货。
6. 合规：涉及财产、功效成分、健康等内容的，结尾保留或补充免责声明；有人身安全隐患的操作保留安全警告。统一中文标点。

稿件：
{body}"""


# ============ 提示词库（系统默认 + 用户自定义，2026-08-20） ============
# 系统默认提示词就是本文件里的常量；用户自定义提示词存 prompt_templates 表。
# 流水线解析顺序：任务创建者在该 (stage, mode) 有「启用」的自定义提示词 → 用之，
# 否则回退系统默认。

STAGE_CATALOG = [
    {"stage": "draft_gen", "label": "正文生成",
     "modes": ["general", "single", "compare"],
     "hint": "任务 query 会追加在提示词之后。"},
    {"stage": "draft_polish", "label": "校稿润色",
     "modes": [None],
     "hint": "用 {body} 引用待校稿件；输出校稿后的最终全文。"
             "LLM 失败时自动沿用原稿，不阻塞流水线。"},
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
    if stage == "draft_polish":
        return DRAFT_POLISH_PROMPT
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
