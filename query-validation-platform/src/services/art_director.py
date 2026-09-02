"""视觉策划（art director，2026-09-02 反模板化：向 ChatGPT 网页工作模式靠拢）。

网页版生图「预设之外」的感觉来自生图前的思考层：先理解内容、为每张图现场做
创意决策，再把决策后的专属请求发给生图模型。本服务即该思考层——page_split
之后、asset_gen 之前用一次 LLM 为 6 页各出一份创意 brief（构图/文字载体/
色调/视觉元素/信息焦点 + 示意图用 title_zone），注入生图提示词替代固定的
6 构图×6 文字形式轮换（词汇表池小、跨任务撞款是模板感根源，见
prompt_versions.get_image_prompt 的 plan_page 参数）。

底座铁律（字体规范/伪汉字禁令/逐字复制/底座质感）仍由代码模板保证，方案只管
创意层。解析失败/LLM 失败 → 返回 None，调用方回退现有轮换，不阻塞出图。
方案随任务冻结（tasks.plan_json，迁移 017）：confirm_gen（待生图）环节人工
确认/编辑/重策划，定点重生成沿用同一方案不漂移。
"""
import json
import traceback

_MODE_LABEL = {
    "general": "通用科普/教程", "single": "单品深度测评", "compare": "双主体对比"}

_PLAN_PROMPT = """你是小红书图文的视觉总监。下面是一套 6 页竖版 3:4 图文卡片的选题、分页文案与已选定的视觉风格。请为每页制定一份创意方案：6 页排版各不相同、构图跟着内容走，不要套用固定模板。

【选题】{query}
【内容类型】{mode_label}
【视觉风格】{style_desc}
【参考图】{refs_note}

输出要求：
1. 只输出严格 JSON，不要输出任何其他文字、解释或 markdown 代码围栏，结构：
{{"pages":[{{"page":1,"composition":"构图版式","text_form":"文字载体","palette":"色调","elements":"视觉元素","focus":"信息焦点","title_zone":"top"}},……共 6 页]}}
2. composition（30-60字）：本页构图与版式——主体占比、文字区位置、视觉动线；6 页互不相同，禁止每页都上文下图。
3. text_form（25字内）：本页文字的呈现载体，可参考但不限于：纯色留白直排双色标题、杂志式细分隔线、主题色胶囊标签、图标加半透明衬底、底部浅色圆角横条、锯齿贴纸气泡；6 页尽量不重复，全套深色底文字框最多出现 1 次。
4. palette（15字内）：本页色调，在整体风格内的细微变化。
5. elements（30字内）：画面视觉元素，与本页文案强相关、具体可画，6 页互不重复。
6. focus（15字内）：本页唯一信息焦点。
7. title_zone：本页文字主区域位置，top/middle/bottom 三选一，6 页尽量错开。
8. 硬性约束：不出现人脸、书籍、带文字的物体；图中文字为简洁中文黑体，方案里不要设计花体/艺术字。

【6 页文案】
{pages}"""

_REPLAN_SUFFIX = """

【人工修改意见】
{feedback}
请在保持整体风格与已定视觉方向不变的前提下，针对上述意见调整方案（没提到的页面不要大改）。"""

# 各字段长度上限（超出截断，防 LLM 啰嗦撑爆生图提示词）
_FIELD_MAX = {"composition": 100, "text_form": 40, "palette": 25,
              "elements": 50, "focus": 25}
_ZONES = ("top", "middle", "bottom")


def normalize_plan(data) -> dict | None:
    """校验/规整 LLM 或人工提交的方案：恰好 6 页、字段齐全为字符串、长度截断、
    title_zone 合法化。不合格返回 None。人工编辑保存与 LLM 输出共用本入口。"""
    try:
        pages = data.get("pages") if isinstance(data, dict) else None
        if not isinstance(pages, list) or len(pages) != 6:
            return None
        out = []
        for i, p in enumerate(pages, start=1):
            if not isinstance(p, dict):
                return None
            row = {"page": i}
            for f, limit in _FIELD_MAX.items():
                v = str(p.get(f) or "").strip()
                if f in ("composition", "focus") and not v:
                    return None  # 构图与信息焦点必填，缺了说明方案没做完
                row[f] = v[:limit]
            zone = str(p.get("title_zone") or "").strip().lower()
            # 缺省按页码错开（与旧轮换同向），非法值回退
            row["title_zone"] = zone if zone in _ZONES else _ZONES[(i - 1) % 3]
            out.append(row)
        return {"pages": out}
    except Exception:  # noqa: BLE001
        return None


def build_plan_prompt(query: str, mode: str, page_bodies: list,
                      style_desc: str = None, has_refs: bool = False,
                      feedback: str = None) -> str:
    pages = "\n".join(f"第{i}页：{b}" for i, b in enumerate(page_bodies, 1))
    prompt = (_PLAN_PROMPT
              .replace("{query}", (query or "").strip())
              .replace("{mode_label}", _MODE_LABEL.get(mode, _MODE_LABEL["general"]))
              .replace("{style_desc}", (style_desc or "（未选定，按通用治愈暖调把握）").strip())
              .replace("{refs_note}",
                       "有实景参考图会融入画面，构图需为实景图留位置" if has_refs
                       else "无参考图，纯 AI 绘制，主体必须直接描绘文案所讲事物")
              .replace("{pages}", pages))
    if feedback:
        prompt += _REPLAN_SUFFIX.replace("{feedback}", feedback.strip())
    return prompt


def finalize_plan(plan: dict, style: str = None, no_text: bool = True,
                  layout_offset: int = 0) -> dict:
    """节点与重策划共用的方案收尾：剥离成本字段、补元信息（风格/模式/时间）、
    确定示意图文字区位置。无字合成模式以代码槽位为准（与 text_composite
    _ZONE_BY_PAGE 合成落版对齐，策划的 title_zone 被覆盖）；有字版保留策划值。"""
    from datetime import datetime, timezone
    out = {k: v for k, v in plan.items() if k != "cost_cny"}
    out["style"] = style
    out["no_text"] = no_text
    out["created_at"] = datetime.now(timezone.utc).isoformat()
    if no_text:
        from src.services.text_composite import _ZONE_BY_PAGE
        for p in out["pages"]:
            slot = ((p["page"] - 1 + layout_offset) % 6) + 1
            p["title_zone"] = _ZONE_BY_PAGE.get(slot, "bottom")
    return out


async def generate_plan(query: str, mode: str, page_bodies: list,
                        style_desc: str = None, has_refs: bool = False,
                        feedback: str = None, llm_call=None) -> dict | None:
    """为 6 页分页文案生成创意方案，返回 {"pages": [...6...], "model": ...}；
    失败返回 None（调用方回退固定轮换，不阻塞出图）。

    feedback：人工重策划意见（confirm_gen 环节「重新策划」或驳回重跑时的审核
    反馈），追加进提示词做定向调整。llm_call 由调用方注入以便测试 mock；
    缺省走 failover 主备通道（与 extract_page_subjects 相同模式）。
    """
    if llm_call is None:
        from src.gateway.failover import call_with_failover, DEEPSEEK_MODEL, KIMI_MODEL

        async def llm_call(prompt):
            return await call_with_failover(prompt, DEEPSEEK_MODEL, KIMI_MODEL,
                                            max_retries=1)
    try:
        bodies = list(page_bodies)[:6]
        while len(bodies) < 6:
            bodies.append("")
        prompt = build_plan_prompt(query, mode, bodies, style_desc=style_desc,
                                   has_refs=has_refs, feedback=feedback)
        r = await llm_call(prompt)
        raw = (r.get("text") or r.get("content") or "").strip()
        data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        plan = normalize_plan(data)
        if plan is None:
            return None
        plan["model"] = r.get("model_version")
        plan["cost_cny"] = r.get("cost_cny", 0.0)
        return plan
    except Exception:
        traceback.print_exc()  # 策划失败不阻塞出图，回退固定构图/文字形式轮换
        return None
