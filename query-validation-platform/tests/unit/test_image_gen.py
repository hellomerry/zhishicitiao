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
