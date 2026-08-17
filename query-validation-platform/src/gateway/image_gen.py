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


async def generate_image(prompt: str, size: str = None,
                         reference_image_urls: list[str] | None = None) -> dict:
    """调用 gpt-image-1.5 生成一张图；reference_image_urls 非空则图生图。"""
    size = size or IMAGE_SIZE
    if reference_image_urls:
        try:
            return await _edit_with_references(prompt, reference_image_urls, size)
        except httpx.HTTPError:
            # 参考图下载失败 → 降级文生图
            return await _generate(prompt, size)
    return await _generate(prompt, size)


async def _generate(prompt: str, size: str) -> dict:
    """文生图：POST /v1/images/generations"""
    url = f"{settings.openai_image_base_url}/v1/images/generations"
    payload = {"model": IMAGE_MODEL, "prompt": prompt, "size": size, "n": 1}
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(url, json=payload, headers=_headers())
        if resp.status_code >= 400:
            raise RuntimeError(f"image gen failed ({resp.status_code}): {resp.text[:400]}")
        data = resp.json()
    return _result(data["data"][0]["url"])


async def _edit_with_references(prompt: str, reference_image_urls: list[str],
                                size: str) -> dict:
    """图生图：POST /v1/images/edits，参考图 multipart 上传。"""
    url = f"{settings.openai_image_base_url}/v1/images/edits"
    files = []
    for i, ref_url in enumerate(reference_image_urls):
        content = await _download_image_bytes(ref_url)
        files.append(("image[]", (f"ref_{i}.png", content, "image/png")))
    data = {"model": IMAGE_MODEL, "prompt": prompt, "size": size,
            "input_fidelity": "high", "n": "1"}
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(url, data=data, files=files, headers=_headers())
        if resp.status_code >= 400:
            raise RuntimeError(f"image edit failed ({resp.status_code}): {resp.text[:400]}")
        j = resp.json()
    return _result(j["data"][0]["url"])


def _result(image_url: str) -> dict:
    return {"image_url": image_url, "hash": hashlib.md5(image_url.encode()).hexdigest(),
            "model_version": IMAGE_MODEL}
