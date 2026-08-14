import httpx
from src.config import settings

# z-image-turbo 生图（阿里百炼 DashScope，中文渲染优，3:4 竖版）
IMAGE_MODEL = "z-image-turbo"
IMAGE_SIZE_3_4 = "864*1152"  # 3:4 竖版


async def generate_image(prompt: str, size: str = IMAGE_SIZE_3_4) -> dict:
    """调用 z-image-turbo 生成一张图，返回 image_url + hash。"""
    url = f"{settings.dashscope_base_url}/services/aigc/multimodal-generation/generation"
    payload = {
        "model": IMAGE_MODEL,
        "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
        "parameters": {"size": size, "prompt_extend": False},
    }
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {settings.dashscope_api_key}"}
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"image gen failed ({resp.status_code}): {resp.text[:400]}")
        data = resp.json()
    choice = data["output"]["choices"][0]
    content = choice["message"]["content"]
    image_url = content[0]["image"] if content and "image" in content[0] else None
    if not image_url:
        raise RuntimeError(f"image generation returned no image: {data}")
    import hashlib
    h = hashlib.md5(image_url.encode()).hexdigest()
    return {"image_url": image_url, "hash": h, "model_version": IMAGE_MODEL}
