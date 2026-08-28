import pytest
from unittest.mock import AsyncMock, patch
import httpx
from src.gateway import image_gen

# `mock_external_calls` autouse fixture 会把模块级 image_gen.generate_image 替换成
# AsyncMock（供 pipeline/集成测试避免真实网络调用）。这里在 import 时保存真实函数，
# 单元测试要测的正是它内部的路由逻辑（_generate vs _edit_with_references）。
_generate_image = image_gen.generate_image


@pytest.mark.asyncio
async def test_generate_text_only_routes_to_generate():
    with patch.object(image_gen, "_generate", new=AsyncMock(return_value={"image_url": "u", "hash": "h", "model_version": "gpt-image-1.5"})) as gen, \
         patch.object(image_gen, "_edit_with_references", new=AsyncMock()) as edit:
        await _generate_image("prompt")
    gen.assert_awaited_once()
    edit.assert_not_called()


@pytest.mark.asyncio
async def test_generate_with_refs_routes_to_edit():
    with patch.object(image_gen, "_edit_with_references", new=AsyncMock(return_value={"image_url": "u", "hash": "h", "model_version": "gpt-image-1.5"})) as edit:
        await _generate_image("prompt", reference_image_urls=["https://x/a.png"])
    edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_ref_download_failure_falls_back_to_generate():
    with patch.object(image_gen, "_edit_with_references",
                      new=AsyncMock(side_effect=httpx.HTTPStatusError("err", request=None, response=None))) as edit, \
         patch.object(image_gen, "_generate", new=AsyncMock(return_value={"image_url": "u", "hash": "h", "model_version": "gpt-image-1.5"})) as gen:
        await _generate_image("prompt", reference_image_urls=["https://x/a.png"])
    gen.assert_awaited_once()


@pytest.mark.asyncio
async def test_mock_image_gen_returns_placeholder(monkeypatch):
    monkeypatch.setattr(image_gen.settings, "mock_image_gen", True)
    with patch.object(image_gen, "_generate", new=AsyncMock()) as gen:
        r = await _generate_image("测试 prompt")
    assert r["model_version"] == "mock"
    assert r["image_url"].startswith("data:image/svg+xml")
    assert r["hash"]
    gen.assert_not_called()


class _FakeResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = "bad request"

    def json(self):
        return self._payload


def _fake_client_cls(seen, reject_quality: bool):
    """模拟网关：reject_quality=True 时对带 quality 的请求返回 400。"""
    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kwargs):
            payload = kwargs.get("json") or kwargs.get("data")
            seen.append(dict(payload))
            if reject_quality and "quality" in payload:
                return _FakeResp(400)
            return _FakeResp(200, {"data": [{"url": "https://img/x.png"}]})

    return FakeClient


@pytest.mark.asyncio
async def test_quality_param_sent_when_supported(monkeypatch):
    """quality=high（2026-08-27 官方建议）正常下发给支持的网关。"""
    monkeypatch.setattr(image_gen.settings, "image_quality", "high")
    monkeypatch.setattr(image_gen.settings, "openai_image_base_url", "https://gw.example/v1")
    seen = []
    monkeypatch.setattr(image_gen.httpx, "AsyncClient",
                        _fake_client_cls(seen, reject_quality=False))
    r = await image_gen._generate("p", "1152x1536")
    assert r["image_url"] == "https://img/x.png"
    assert len(seen) == 1 and seen[0]["quality"] == "high"


@pytest.mark.asyncio
async def test_quality_param_fallback_on_400(monkeypatch):
    """网关不认识 quality 返回 400 时，自动去掉该参数重试一次，不致整体生图失败。"""
    monkeypatch.setattr(image_gen.settings, "image_quality", "high")
    monkeypatch.setattr(image_gen.settings, "openai_image_base_url", "https://gw.example/v1")
    seen = []
    monkeypatch.setattr(image_gen.httpx, "AsyncClient",
                        _fake_client_cls(seen, reject_quality=True))
    r = await image_gen._generate("p", "1152x1536")
    assert r["image_url"] == "https://img/x.png"
    assert len(seen) == 2
    assert seen[0]["quality"] == "high" and "quality" not in seen[1]


# ---------- Gemini provider（2026-08-28 fusionaix generateContent 协议）----------


def _gemini_client_cls(seen, payload):
    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kwargs):
            seen.append({"url": url, "json": kwargs.get("json"),
                         "headers": kwargs.get("headers")})
            return _FakeResp(200, payload)

    return FakeClient


_GEMINI_OK = {
    "candidates": [{"content": {"parts": [
        {"text": "说明文字"},
        {"inlineData": {"mimeType": "image/png",
                        "data": "aGVsbG8taW1n"}},  # b"hello-img"
    ]}}],
}


@pytest.mark.asyncio
async def test_gemini_generate_request_and_response(monkeypatch):
    """gemini provider：参考图内联 inlineData + 提示词同请求；响应取含
    inlineData 的 part，返回 data URI + 内容 md5。"""
    import base64
    monkeypatch.setattr(image_gen.settings, "gemini_image_base_url",
                        "https://fusion.example")
    monkeypatch.setattr(image_gen.settings, "openai_image_api_key", "sk-fusion-x")
    monkeypatch.setattr(image_gen.settings, "gemini_api_key", "")
    monkeypatch.setattr(image_gen.settings, "gemini_image_model",
                        "gemini-3-pro-image")
    monkeypatch.setattr(image_gen.settings, "gemini_image_size", "2K")
    seen = []
    monkeypatch.setattr(image_gen.httpx, "AsyncClient",
                        _gemini_client_cls(seen, _GEMINI_OK))
    monkeypatch.setattr(image_gen, "_download_image_bytes",
                        AsyncMock(return_value=(b"refbytes", "image/jpeg")))
    r = await image_gen._gemini_generate(
        "画一杯冰牛奶", ["https://x/ref1.jpg"], "1152x1536")
    req = seen[0]
    assert req["url"] == ("https://fusion.example/v1beta/models/"
                          "gemini-3-pro-image:generateContent")
    assert req["headers"]["Authorization"] == "Bearer sk-fusion-x"
    parts = req["json"]["contents"][0]["parts"]
    assert parts[0]["inlineData"]["mimeType"] == "image/jpeg"
    assert base64.b64decode(parts[0]["inlineData"]["data"]) == b"refbytes"
    assert parts[1] == {"text": "画一杯冰牛奶"}
    cfg = req["json"]["generationConfig"]
    assert cfg["imageConfig"] == {"aspectRatio": "3:4", "imageSize": "2K"}
    assert r["image_url"] == "data:image/png;base64,aGVsbG8taW1n"
    import hashlib
    assert r["hash"] == hashlib.md5(b"hello-img").hexdigest()
    assert r["model_version"] == "gemini-3-pro-image"


@pytest.mark.asyncio
async def test_gemini_provider_routing(monkeypatch):
    """image_provider=gemini 时 generate_image 走 _gemini_generate（有无参考图都一样，
    Gemini 原生多模态无独立 edits 端点）。"""
    monkeypatch.setattr(image_gen.settings, "image_provider", "gemini")
    monkeypatch.setattr(image_gen.settings, "mock_image_gen", False)
    with patch.object(image_gen, "_gemini_generate",
                      new=AsyncMock(return_value={"image_url": "u", "hash": "h",
                                                  "model_version": "gemini"})) as gem, \
         patch.object(image_gen, "_generate", new=AsyncMock()) as gen, \
         patch.object(image_gen, "_edit_with_references", new=AsyncMock()) as edit:
        await _generate_image("p")
        await _generate_image("p", reference_image_urls=["https://x/a.png"])
    assert gem.await_count == 2
    gen.assert_not_called()
    edit.assert_not_called()
    monkeypatch.setattr(image_gen.settings, "image_provider", "openai_images")


def test_aspect_of():
    assert image_gen._aspect_of("1152x1536") == "3:4"
    assert image_gen._aspect_of("1024x1024") == "1:1"
    assert image_gen._aspect_of("bad") == "3:4"


def test_cost_per_image_by_provider(monkeypatch):
    monkeypatch.setattr(image_gen.settings, "image_provider", "gemini")
    monkeypatch.setattr(image_gen.settings, "gemini_image_cost_per_image_cny", 0.6)
    monkeypatch.setattr(image_gen.settings, "image_cost_per_image_cny", 0.2)
    assert image_gen.cost_per_image() == 0.6
    monkeypatch.setattr(image_gen.settings, "image_provider", "openai_images")
    assert image_gen.cost_per_image() == 0.2


@pytest.mark.asyncio
async def test_fetch_image_bytes_data_uri():
    """fetch_image_bytes 支持 Gemini 适配层返回的 data URI（本地解码不进网络）。"""
    from src.gateway.ocr import fetch_image_bytes
    data, ctype = await fetch_image_bytes("data:image/png;base64,aGVsbG8taW1n")
    assert data == b"hello-img" and ctype == "image/png"
