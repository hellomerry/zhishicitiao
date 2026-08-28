import asyncio
import hashlib
import httpx
from src.config import settings

IMAGE_MODEL = settings.image_model
IMAGE_SIZE = settings.image_size


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.openai_image_api_key}"}


async def _download_image_bytes(url: str) -> tuple:
    """取参考图字节，返回 (bytes, content_type)。

    复用 fetch_image_bytes：支持本地产出（/static/... 读磁盘）、openox 签名
    内联 URL 本地解码、远程带浏览器 UA/Referer 防盗链。直接用 httpx 裸 GET
    会对本地路径抛 UnsupportedProtocol、对防盗链图床 403，导致图生图被静默
    降级成文生图（2026-08-20 对比模式实景图未生效的根因）。
    """
    from src.gateway.ocr import fetch_image_bytes
    return await fetch_image_bytes(url)


def _mock_result(prompt: str) -> dict:
    """开发阶段模拟生图：返回占位 SVG（data URI），不调用真实 API。"""
    h = hashlib.md5(prompt.encode()).hexdigest()
    svg = (
        "data:image/svg+xml;utf8,"
        f"<svg xmlns='http://www.w3.org/2000/svg' width='576' height='768'>"
        f"<rect width='100%' height='100%' fill='%23{h[:6]}'/>"
        f"<text x='50%' y='50%' font-size='40' fill='white' text-anchor='middle'"
        f" font-family='sans-serif'>MOCK {h[:4]}</text></svg>"
    )
    return {"image_url": svg, "hash": h, "model_version": "mock"}


async def generate_image(prompt: str, size: str = None,
                         reference_image_urls: list[str] | None = None,
                         max_retries: int = 3) -> dict:
    """按 settings.image_provider 生成一张图；reference_image_urls 非空则图生图。

    mock_image_gen 开启时返回占位图；否则瞬时错误（SSL/502/400）做退避重试。
    provider=gemini 时走 Gemini generateContent 协议（原生多模态，参考图与提示词
    同请求内联，无独立 edits 端点）。
    """
    if settings.mock_image_gen:
        return _mock_result(prompt)
    size = size or IMAGE_SIZE
    for attempt in range(max_retries):
        try:
            if settings.image_provider == "gemini":
                return await _gemini_generate(prompt, reference_image_urls, size)
            if reference_image_urls:
                try:
                    return await _edit_with_references(prompt, reference_image_urls, size)
                except Exception as e:  # noqa: BLE001
                    # 参考图下载失败 / edits 接口报错 → 降级文生图，必须留痕，
                    # 静默降级会让"对比模式用实景图"失效且无人察觉
                    print(f"[image_gen] 图生图失败，降级文生图: {type(e).__name__}: {e}",
                          flush=True)
                    return await _generate(prompt, size)
            return await _generate(prompt, size)
        except Exception as e:  # noqa: BLE001
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(2 * (2 ** attempt))


def cost_per_image() -> float:
    """当前 provider 的单张生图成本（元）。Gemini 与 gpt-image-2 单价不同，
    成本记账统一走这里，不要在调用方直接读 settings.image_cost_per_image_cny。"""
    if settings.image_provider == "gemini":
        return settings.gemini_image_cost_per_image_cny
    return settings.image_cost_per_image_cny


def _aspect_of(size: str) -> str:
    """1152x1536 → 3:4（Gemini imageConfig.aspectRatio 用宽高比，不用像素尺寸）。"""
    from fractions import Fraction
    try:
        w, h = (int(x) for x in size.lower().split("x"))
        f = Fraction(w, h)
        return f"{f.numerator}:{f.denominator}"
    except Exception:
        return "3:4"


async def _gemini_generate(prompt: str, reference_image_urls: list[str] | None,
                           size: str) -> dict:
    """Gemini generateContent 生图（2026-08-28 fusionaix 实测接入）：
    POST {base}/v1beta/models/{model}:generateContent，参考图以 inlineData 内联
    进 contents（原生多模态，无独立 edits 端点）；响应取 candidates[0] 里含
    inlineData 的 part，图片字节以 data URI 返回（fetch_image_bytes 支持解码，
    下游 _dedupe_and_validate 随即本地化落盘，data URI 只在内存中转）。"""
    import base64
    base = (settings.gemini_image_base_url
            or settings.openai_image_base_url.removesuffix("/v1")).rstrip("/")
    key = settings.gemini_api_key or settings.openai_image_api_key
    model = settings.gemini_image_model
    parts = []
    for ref_url in (reference_image_urls or []):
        content, ctype = await _download_image_bytes(ref_url)
        parts.append({"inlineData": {"mimeType": ctype,
                                     "data": base64.b64encode(content).decode()}})
    parts.append({"text": prompt})
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": _aspect_of(size),
                            "imageSize": settings.gemini_image_size},
        },
    }
    url = f"{base}/v1beta/models/{model}:generateContent"
    # 图片生成可能数分钟（网关文档建议 10 分钟超时）
    async with httpx.AsyncClient(timeout=600) as client:
        resp = await client.post(url, json=payload,
                                 headers={"Authorization": f"Bearer {key}"})
        if resp.status_code >= 400:
            raise RuntimeError(
                f"gemini image gen failed ({resp.status_code}): {resp.text[:400]}")
        data = resp.json()
    for cand in data.get("candidates", []):
        for part in (cand.get("content") or {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                raw = base64.b64decode(inline["data"])
                mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                return {"image_url": f"data:{mime};base64,{inline['data']}",
                        "hash": hashlib.md5(raw).hexdigest(),
                        "model_version": model}
    raise RuntimeError(f"gemini response missing inlineData: {str(data)[:300]}")


async def _post_with_quality_fallback(client: httpx.AsyncClient, url: str,
                                      payload: dict, files=None) -> httpx.Response:
    """带 quality 参数发请求；网关（如 openox 转发）不认识该参数返回 400 时，
    去掉 quality 重试一次并留痕——避免升级 quality=high 后在不支持的网关上整体生图失败。"""
    if settings.image_quality:
        payload["quality"] = settings.image_quality

    def _kwargs(p: dict) -> dict:
        if files is None:
            return {"json": p, "headers": _headers()}
        return {"data": p, "files": files, "headers": _headers()}

    resp = await client.post(url, **_kwargs(payload))
    if resp.status_code >= 400 and "quality" in payload:
        print(f"[image_gen] 网关拒绝 quality 参数（{resp.status_code}），去掉后重试一次",
              flush=True)
        payload.pop("quality")
        resp = await client.post(url, **_kwargs(payload))
    return resp


async def _generate(prompt: str, size: str) -> dict:
    """文生图：POST /v1/images/generations"""
    url = f"{settings.openai_image_base_url}/images/generations"
    payload = {"model": IMAGE_MODEL, "prompt": prompt, "size": size, "n": 1,
               "response_format": "url"}
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await _post_with_quality_fallback(client, url, payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"image gen failed ({resp.status_code}): {resp.text[:400]}")
        data = resp.json()
    u = data["data"][0].get("url")
    if not u:
        raise RuntimeError(f"image response missing url field: {str(data)[:200]}")
    return _result(u)


async def _edit_with_references(prompt: str, reference_image_urls: list[str],
                                size: str) -> dict:
    """图生图：POST /v1/images/edits，参考图 multipart 上传。"""
    url = f"{settings.openai_image_base_url}/images/edits"
    files = []
    for i, ref_url in enumerate(reference_image_urls):
        content, ctype = await _download_image_bytes(ref_url)
        ext = {"image/png": "png", "image/jpeg": "jpg",
               "image/webp": "webp"}.get(ctype, "png")
        files.append(("image[]", (f"ref_{i}.{ext}", content, ctype)))
    # gpt-image-2 编辑时自动高保真，传 input_fidelity 会返回 400，故不传
    data = {"model": IMAGE_MODEL, "prompt": prompt, "size": size,
            "n": "1", "response_format": "url"}
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await _post_with_quality_fallback(client, url, data, files=files)
        if resp.status_code >= 400:
            raise RuntimeError(f"image edit failed ({resp.status_code}): {resp.text[:400]}")
        j = resp.json()
    u = j["data"][0].get("url")
    if not u:
        raise RuntimeError(f"image response missing url field: {str(j)[:200]}")
    return _result(u)


def _result(image_url: str) -> dict:
    return {"image_url": image_url, "hash": hashlib.md5(image_url.encode()).hexdigest(),
            "model_version": IMAGE_MODEL}
