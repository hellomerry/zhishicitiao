"""风格模板开局 + 样例学风格 + 学习降级加权的测试（2026-09-01「不同用户
不同风格」方案）。

设计原则：风格差异来自用户显式选择（即时生效）；偏好学习只在未钉选默认
风格时参与（加权上限 2.0 + 每 5 条 1 条探索位），永不替换用户钉选。
"""
import io
import uuid

import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch
from sqlalchemy import text

from src.api.main import app
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.services import style_pick
from src.services.style_pick import pick_image_style


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _uniq() -> str:
    return uuid.uuid4().hex[:8]


async def _make_user(role="A") -> tuple:
    """建在职用户，返回 (用户名, uid)。"""
    name = f"u-{_uniq()}"
    async with SessionLocal() as s:
        await s.execute(text("INSERT INTO users (name, role) VALUES (:n, :r)"),
                        {"n": name, "r": role})
        await s.commit()
        uid = (await s.execute(text("SELECT id FROM users WHERE name = :n"),
                               {"n": name})).scalar()
    return name, str(uid)


async def _make_tasks(uid, style: str, n: int, status: str = "approved"):
    async with SessionLocal() as s:
        for _ in range(n):
            s.add(Task(idempotency_key=f"ob-{_uniq()}", query="q",
                       content_type="x", mode="general", created_by=uid,
                       gen_image_style=style, status=status))
        await s.commit()


# ---------- 风格模板开局 ----------

@pytest.mark.asyncio
async def test_templates_list_builtin():
    async with _client() as c:
        r = await c.get("/api/styles/templates")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == len(style_pick.IMAGE_STYLE_LIBRARY)
    assert all(i["style_name"] and i["description"] for i in items)


@pytest.mark.asyncio
async def test_onboarding_state_and_clone_flow():
    user, uid = await _make_user()
    async with _client() as c:
        # 新用户：需要开局引导
        r = await c.get(f"/api/styles/onboarding_state?actor={user}")
        assert r.json()["needs_onboarding"] is True
        # 克隆 2 个模板 + 钉选默认
        r = await c.post("/api/styles/clone_templates", json={
            "actor": user, "style_names": ["自然写实暖调", "杂志编辑"],
            "pin": "自然写实暖调"})
        assert r.status_code == 200
        assert sorted(r.json()["cloned"]) == ["杂志编辑", "自然写实暖调"]
        # 个人库有 2 条、默认已钉、不再需要引导
        items = (await c.get(f"/api/styles?actor={user}")).json()["items"]
        mine = [i for i in items if i["scope"] == "mine"]
        assert len(mine) == 2
        st = (await c.get(f"/api/styles/onboarding_state?actor={user}")).json()
        assert st["needs_onboarding"] is False
        assert st["default_style"] == "自然写实暖调"
        # 用户改过自己的条目后再次克隆同名 → 跳过、保留用户版本
        await c.post(f"/api/styles?actor={user}", json={
            "style_name": "杂志编辑", "keywords": "时尚", "description": "用户改过的描述"})
        r = await c.post("/api/styles/clone_templates", json={
            "actor": user, "style_names": ["杂志编辑"]})
        assert r.json()["skipped"] == ["杂志编辑"]
        items = (await c.get(f"/api/styles?actor={user}")).json()["items"]
        mine = {i["style_name"]: i for i in items if i["scope"] == "mine"}
        assert mine["杂志编辑"]["description"] == "用户改过的描述"


@pytest.mark.asyncio
async def test_clone_templates_validation():
    user, uid = await _make_user()
    async with _client() as c:
        # 不存在的模板名 → 422
        r = await c.post("/api/styles/clone_templates", json={
            "actor": user, "style_names": ["不存在的风格"]})
        assert r.status_code == 422
        # pin 不在 style_names 内 → 422
        r = await c.post("/api/styles/clone_templates", json={
            "actor": user, "style_names": ["治愈暖彩"], "pin": "复古印刷"})
        assert r.status_code == 422


# ---------- 样例学风格 ----------

class _LearnResp:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content":
                    '{"style_name": "清新手账风", "keywords": "手账,文具,日常",'
                    ' "description": "浅绿纸纹底、手绘贴纸、细线分隔"}'}}],
                "usage": {}}


class _LearnClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return _LearnResp()


def _png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (100, 100), (200, 220, 200)).save(buf, "PNG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_learn_from_images_returns_draft():
    user, uid = await _make_user()
    import src.api.styles as styles_api
    with patch.object(styles_api.httpx, "AsyncClient", _LearnClient):
        async with _client() as c:
            r = await c.post("/api/styles/learn",
                             data={"actor": user},
                             files=[("files", ("a.png", _png(), "image/png"))])
    assert r.status_code == 200
    j = r.json()
    assert j["style_name"] == "清新手账风"
    assert "手账" in j["keywords"] and "纸纹" in j["description"]


@pytest.mark.asyncio
async def test_learn_from_images_oversize_rejected():
    user, uid = await _make_user()
    big = b"\x89PNG" + b"0" * (16 * 1024 * 1024)
    async with _client() as c:
        r = await c.post("/api/styles/learn", data={"actor": user},
                         files=[("files", ("big.png", big, "image/png"))])
    assert r.status_code == 422


# ---------- 学习降级加权 ----------

async def _no_default(uid):
    async with SessionLocal() as s:
        await s.execute(text("UPDATE users SET default_style = NULL"
                             " WHERE id = :u"), {"u": uid})
        await s.commit()


@pytest.mark.asyncio
async def test_weighted_keyword_fallback_prefers_history():
    """未钉选用户：关键词兜底按 命中数×权重 评分，历史高权重风格胜出。"""
    user, uid = await _make_user()
    await _make_tasks(uid, "复古印刷", 4, status="approved")

    async def failing_llm(prompt):
        raise RuntimeError("llm down")

    # 「科普」命中自然写实暖调、「复古」命中复古印刷，各 1 次；
    # 复古印刷带历史权重（4 任务全通过 → ~1.9x）→ 加权后胜出
    name, desc = await pick_image_style("科普复古选题", "", owner_id=uid,
                                        llm_call=failing_llm)
    assert name == "复古印刷"


@pytest.mark.asyncio
async def test_weights_sort_llm_library_listing():
    """未钉选用户：LLM 候选清单按历史权重降序（高权重风格排前温和引导）。"""
    user, uid = await _make_user()
    await _make_tasks(uid, "复古印刷", 3, status="approved")
    seen = {}

    async def capture_llm(prompt):
        seen["prompt"] = prompt
        return {"text": "自然写实暖调"}  # LLM 的选择仍被尊重

    name, _ = await pick_image_style("校园生活", "", owner_id=uid,
                                     llm_call=capture_llm)
    assert name == "自然写实暖调"
    lib = seen["prompt"]
    assert lib.index("复古印刷") < lib.index("治愈暖彩")


@pytest.mark.asyncio
async def test_exploration_slot_skips_llm():
    """探索位：全量任务数每满 5 的倍数，下一条任务从权重下半区直选、不调 LLM。"""
    user, uid = await _make_user()
    await _make_tasks(uid, "复古印刷", 5, status="approved")
    called = {"n": 0}

    async def counting_llm(prompt):
        called["n"] += 1
        return {"text": "复古印刷"}

    name, desc = await pick_image_style("q", "", owner_id=uid,
                                        llm_call=counting_llm)
    assert called["n"] == 0  # 探索位不走 LLM
    assert name != "复古印刷"  # 从下半区选，必不是唯一高权重的复古印刷
    assert name in {n for n, _, _ in style_pick.IMAGE_STYLE_LIBRARY}


@pytest.mark.asyncio
async def test_pinned_default_bypasses_learning():
    """钉选默认的用户：学习加权完全不参与，默认直通（显式选择永远优先）。"""
    user, uid = await _make_user()
    await _make_tasks(uid, "复古印刷", 10, status="approved")
    async with SessionLocal() as s:
        await s.execute(text("UPDATE users SET default_style = '治愈暖彩'"
                             " WHERE id = :u"), {"u": uid})
        await s.commit()

    async def boom(prompt):
        raise AssertionError("钉选用户不应调 LLM")

    name, _ = await pick_image_style("q", "", owner_id=uid, llm_call=boom)
    assert name == "治愈暖彩"


@pytest.mark.asyncio
async def test_style_variants_field_roundtrip():
    """变体轴字段（迁移 016）：保存/读取/更新；清空变 None 回退内置变体池。"""
    user, uid = await _make_user()
    async with _client() as c:
        r = await c.post(f"/api/styles?actor={user}", json={
            "style_name": "带变体风", "keywords": "k", "description": "d",
            "variants": "强调色用砖红；装饰用波点"})
        assert r.status_code == 200
        items = (await c.get(f"/api/styles?actor={user}")).json()["items"]
        mine = [i for i in items if i["scope"] == "mine"][0]
        assert mine["variants"] == "强调色用砖红；装饰用波点"
        # 更新清空 variants → 返回空串（DB 存 None，回退内置变体池）
        await c.post(f"/api/styles?actor={user}", json={
            "style_name": "带变体风", "keywords": "k", "description": "d",
            "variants": ""})
        items = (await c.get(f"/api/styles?actor={user}")).json()["items"]
        assert [i for i in items if i["scope"] == "mine"][0]["variants"] == ""
