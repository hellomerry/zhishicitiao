PROMPT_VERSIONS = {
    "draft_v1": "请你以小红书博主的写作风格及模式，结合权威可靠信源的数据库，创作一篇图文内容。要求：简洁清晰、结构完整、总分总结构、每段加小标题、400-700字、无绝对化表述、无emoji、中文标点。",
    "page_split_v1": "对文章进行精简和拆分，总文字严格控制到350字以内，包括封面和每一页的文字内容，适合放在图上，每个部分一段话。",
    "evidence_v1": "提取这段话中可验证的事实点（数值、单位、年份、定义、引用、因果），每个事实点标注风险等级。",
}


def get_prompt(name: str, version: str = None) -> str:
    if version:
        key = f"{name}_{version}"
        if key in PROMPT_VERSIONS:
            return PROMPT_VERSIONS[key]
    return PROMPT_VERSIONS[f"{name}_v1"]
