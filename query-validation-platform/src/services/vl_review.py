"""VL 视觉二审（2026-09-01，借鉴 8003 ai_review.py）：生图后 qwen-vl 逐页视觉审核。

OCR 文字相似度关卡（nodes._text_quality_gate）只能发现「文字扭曲」，
发现不了：文字量过载/缺失、实景图嵌入不协调（大小/位置/对比度/遮挡主体）。
本模块在 OCR 关卡后再加一层视觉审核，不达标时把 VL 给的「一句调整建议」
拼进生图提示词自动重生成（最多 settings.vl_review_max_rounds 轮），仍败在
model_version 打 |vl_flag 标记交人工审核。VL 调用失败默认通过、不误杀。

与 8003 原版差异：不挂 benchmark_cases 截图对照（我们没有截图库），
VL 模型独立配置（qwen-vl-ocr 是 OCR 专用模型，判断类任务用 qwen3-vl-flash）。
"""
import base64
import json

import httpx

from src.config import settings
from src.gateway.cost_tracker import estimate_cost
from src.gateway.ocr import fetch_image_bytes

_VL_REVIEW_PROMPT = """你是图文交付质量审核员。下面是第 {page} 页交付配图与其目标页文案。逐项审核后只输出 JSON：

【页文案】{page_text}
【本页应有实景参考图嵌入画面：{ref_mode}】

审核项：
1. text_ok：图中文字是否正确（有无伪汉字/异体变形/乱码/明显错字）；
2. text_amount_ok：图中文字数量是否协调（是否信息过载堆太多字，或该有的字缺失）；
3. ref_ok：实景图嵌入是否协调（大小/位置/对比度/不遮挡主体；无实景图时此项给 true）。

输出 JSON：{{"text_ok": true/false, "text_amount_ok": true/false, "ref_ok": true/false,
  "issues": ["问题1"], "suggest": "一句具体的生图调整建议（怎么改提示词）"}}"""

_FLAG_KEYS = ("text_ok", "text_amount_ok", "ref_ok")


async def vl_review_image(image_url: str, page_text: str, page: int,
                          ref_mode: bool) -> dict:
    """qwen-vl 视觉审核一页配图。返回 {pass, issues, suggest, flags, cost_cny}；
    任何异常（网络/解析/非 200）默认通过不误杀，cost_cny 记 0。"""
    ok = {"pass": True, "issues": [], "suggest": "", "flags": {}, "cost_cny": 0.0}
    try:
        data, ctype = await fetch_image_bytes(image_url)
        mime = ctype if ctype.startswith("image/") else "image/png"
        data_url = f"data:{mime};base64," + base64.b64encode(data).decode()
        payload = {
            "model": settings.vl_review_model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": _VL_REVIEW_PROMPT.format(
                    page=page, page_text=(page_text or "")[:120],
                    ref_mode="是" if ref_mode else "否")},
            ]}],
            "max_tokens": 600,
        }
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                f"{settings.ocr_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {settings.dashscope_api_key}"},
                json=payload)
            if resp.status_code != 200:
                return ok
            body = resp.json()
        usage = body.get("usage") or {}
        ok["cost_cny"] = estimate_cost(settings.vl_review_model,
                                       usage.get("prompt_tokens", 0),
                                       usage.get("completion_tokens", 0))
        content = body["choices"][0]["message"]["content"].strip()
        raw = content.strip("`").lstrip("json").strip()
        j = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        flags = {k: bool(j.get(k, True)) for k in _FLAG_KEYS}
        ok.update({
            "pass": all(flags.values()),
            "issues": [str(i)[:100] for i in j.get("issues", [])],
            "suggest": str(j.get("suggest", ""))[:200],
            "flags": flags,
        })
        return ok
    except Exception:  # noqa: BLE001 —— 审核服务异常不阻塞出图
        return ok
