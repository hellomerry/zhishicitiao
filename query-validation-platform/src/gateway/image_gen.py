import asyncio
import hashlib
import httpx
from src.config import settings

IMAGE_MODEL = settings.image_model
IMAGE_SIZE = settings.image_size


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.openai_image_api_key}"}


async def _download_image_bytes(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


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
    """调用 gpt-image-2 生成一张图；reference_image_urls 非空则图生图。

    mock_image_gen 开启时返回占位图；否则瞬时错误（SSL/502/400）做退避重试。
    """
    if settings.mock_image_gen:
        return _mock_result(prompt)
    size = size or IMAGE_SIZE
    for attempt in range(max_retries):
        try:
            if reference_image_urls:
                try:
                    return await _edit_with_references(prompt, reference_image_urls, size)
                except httpx.HTTPError:
                    # 参考图下载失败 → 降级文生图
                    return await _generate(prompt, size)
            return await _generate(prompt, size)
        except Exception as e:  # noqa: BLE001
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(2 * (2 ** attempt))


async def _generate(prompt: str, size: str) -> dict:
    """文生图：POST /v1/images/generations"""
    url = f"{settings.openai_image_base_url}/images/generations"
    payload = {"model": IMAGE_MODEL, "prompt": prompt, "size": size, "n": 1,
               "response_format": "url"}
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(url, json=payload, headers=_headers())
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
        content = await _download_image_bytes(ref_url)
        files.append(("image[]", (f"ref_{i}.png", content, "image/png")))
    # gpt-image-2 编辑时自动高保真，传 input_fidelity 会返回 400，故不传
    data = {"model": IMAGE_MODEL, "prompt": prompt, "size": size,
            "n": "1", "response_format": "url"}
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(url, data=data, files=files, headers=_headers())
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
