"""vl_review_image 单元测试：JSON 解析 + 失败默认通过不误杀（fail-open）。

背景：2026-09-01 借鉴 8003 ai_review，生图后 VL 视觉二审（文字过载/
实景嵌入协调性），VL 服务异常必须不阻塞出图。
"""
import pytest
from unittest.mock import AsyncMock, patch

from src.services import vl_review
from src.services.vl_review import vl_review_image


class _Resp:
    def __init__(self, status_code=200, content="{}"):
        self.status_code = status_code
        self._content = content
        self.text = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


class _Client:
    resp = _Resp()

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return type(self).resp


def _patch_stack(resp):
    return (
        patch.object(vl_review, "fetch_image_bytes",
                     new=AsyncMock(return_value=(b"\x89PNG fake", "image/png"))),
        patch.object(vl_review.httpx, "AsyncClient", _Client),
        patch.object(_Client, "resp", resp),
    )


@pytest.mark.asyncio
async def test_vl_review_parse_fail_verdict():
    """VL 判不达标：pass=False、带 issues/suggest/flags、有成本。"""
    content = ('{"text_ok": true, "text_amount_ok": false, "ref_ok": true,'
               ' "issues": ["文字过载堆太多字"], "suggest": "精简到3行以内"}')
    p1, p2, p3 = _patch_stack(_Resp(200, content))
    with p1, p2, p3:
        r = await vl_review_image("/static/generated/x.png", "页文案", 1, True)
    assert r["pass"] is False
    assert r["flags"]["text_amount_ok"] is False
    assert r["issues"] == ["文字过载堆太多字"]
    assert "精简" in r["suggest"]


@pytest.mark.asyncio
async def test_vl_review_pass_verdict():
    content = ('{"text_ok": true, "text_amount_ok": true, "ref_ok": true,'
               ' "issues": [], "suggest": ""}')
    p1, p2, p3 = _patch_stack(_Resp(200, content))
    with p1, p2, p3:
        r = await vl_review_image("/static/generated/x.png", "页文案", 1, False)
    assert r["pass"] is True


@pytest.mark.asyncio
async def test_vl_review_http_error_fail_open():
    """VL 接口非 200：默认通过不误杀。"""
    p1, p2, p3 = _patch_stack(_Resp(500, "boom"))
    with p1, p2, p3:
        r = await vl_review_image("/static/generated/x.png", "页文案", 1, True)
    assert r["pass"] is True
    assert r["cost_cny"] == 0.0


@pytest.mark.asyncio
async def test_vl_review_bad_json_fail_open():
    """VL 返回非 JSON：默认通过不误杀。"""
    p1, p2, p3 = _patch_stack(_Resp(200, "这不是JSON"))
    with p1, p2, p3:
        r = await vl_review_image("/static/generated/x.png", "页文案", 1, True)
    assert r["pass"] is True


@pytest.mark.asyncio
async def test_vl_review_fetch_error_fail_open():
    """取图失败（网络/磁盘）：默认通过不误杀。"""
    with patch.object(vl_review, "fetch_image_bytes",
                      new=AsyncMock(side_effect=RuntimeError("down"))):
        r = await vl_review_image("/static/generated/x.png", "页文案", 1, True)
    assert r["pass"] is True
