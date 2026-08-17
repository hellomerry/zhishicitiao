"""提示词版本库：按 (用途, mode) 提供正文/生图提示词。"""

_SHARED_IMAGE_STYLE = (
    "竖版3:4图文卡片，一级/二级标题与正文字号对应。"
    "主体清晰不被遮挡、展现完整主体不裁剪关键特征。"
    "坚韧治愈风、高清、极简高级，背景不太白也不太暗。"
    "所有文字必须使用标准可读中文黑体，禁止艺术化变形、阴影、描边、透视扭曲，"
    "正文统一基线对齐、可印刷级清晰；图中文字不超过100字，不出现字号过小的文字，"
    "不要出现「封面/第X页」等字样。排版不要模板化（每页排版不同），"
    "图片元素不与前页重复。不出现人脸、书籍等元素，尽量不出现带文字的物体。"
)

DRAFT_PROMPTS = {
    "general": "请你以小红书博主的写作风格及模式，结合权威可靠信源的数据库，创作一篇图文内容。要求：简洁清晰、结构完整、总分总结构、每段加小标题、400-700字、无绝对化表述、无emoji、中文标点。",
    "single": "请你以小红书博主的写作风格，结合权威可靠信源的数据库，创作一篇单品深度测评图文。围绕单一产品/事物展开，依次讲透：它是什么、原理或关键参数、实测体验、优点、局限、安全/使用提醒、适合谁。要求：简洁清晰、总分总结构、每段加小标题、400-700字、无绝对化表述、无emoji、中文标点，事实数据需有信源支撑。",
    "compare": "请你以小红书博主的写作风格，结合权威可靠信源的数据库，创作一篇对比类图文。客观对比两个主体（产品/学校/方案等），平分笔墨，逐维度列出各自的事实参数、优劣与适用场景，最后给出取舍建议。要求：简洁清晰、总分总结构、每段加小标题、400-700字、无绝对化表述、无emoji、中文标点，事实数据需有信源支撑，不偏袒任何一方。",
}

IMAGE_PROMPTS = {
    "general": "通用科普/教程配图，纯 AI 生成、无参考图。" + _SHARED_IMAGE_STYLE + "本页文案：{page_body}",
    "single": "单品评测配图，将提供的参考实景图融入画面：去水印、去人物、实景图不重复、每页实景图不宜过多以免杂乱；不删减参考图上的文字，也不额外添加其他图片。" + _SHARED_IMAGE_STYLE + "本页文案：{page_body}",
    "compare": "对比类配图，将两个主体的参考实景图融入画面，每页尽量同时呈现两个主体做对比（参考图顺序不能乱）：去水印、去人物、实景图不重复。" + _SHARED_IMAGE_STYLE + "本页文案：{page_body}",
}

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


def get_image_prompt(mode: str, page_body: str) -> str:
    template = IMAGE_PROMPTS.get(mode, IMAGE_PROMPTS["general"])
    return template.replace("{page_body}", page_body)
