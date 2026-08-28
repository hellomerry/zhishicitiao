"""任务级生图模型选择（2026-08-28 用户要求）：
默认 gpt-image-2（openai_images），其它模型（gemini）仅在用户手动选择时生效。
tasks.image_provider（迁移 010）为空走全局配置；asset_gen/定点重生成/成本取价
均按任务值。"""
import uuid

import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from src.api.main import app
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.models.drafts import PageCopy
from src.pipeline.nodes import node_asset_gen


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _make_user(role="C"):
    from sqlalchemy import text as _text
    from src.api.auth import hash_password
    name = f"u-{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as s:
        await s.execute(_text(
            "INSERT INTO users (name, role, password_hash) VALUES (:n, :r, :p)"),
            {"n": name, "r": role, "p": hash_password("pw-123456")})
        await s.commit()
    return name


async def _owned_task(owner_name, status="confirm_gen", provider=None):
    from sqlalchemy import text as _text
    async with SessionLocal() as s:
        task = Task(idempotency_key=f"m-{uuid.uuid4().hex[:8]}",
                    query="冰牛奶高脚杯怎么做", content_type="generic",
                    mode="general", status=status, image_provider=provider)
        s.add(task)
        await s.commit()
        await s.refresh(task)
        tid = task.id
        uid = (await s.execute(_text(
            "SELECT id FROM users WHERE name = :n"), {"n": owner_name})).scalar_one()
        await s.execute(_text(
            "UPDATE tasks SET created_by = :u WHERE id = :t"), {"u": uid, "t": tid})
        await s.commit()
    return tid


@pytest.mark.asyncio
async def test_set_image_model_ok_and_persisted():
    owner = await _make_user(role="admin")
    tid = await _owned_task(owner)
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{tid}/image_model",
                          json={"actor": owner, "provider": "gemini"})
        assert r.status_code == 200
        assert r.json()["image_provider"] == "gemini"
    async with SessionLocal() as s:
        task = (await s.execute(select(Task).where(Task.id == tid))).scalar_one()
        assert task.image_provider == "gemini"


@pytest.mark.asyncio
async def test_set_image_model_bad_provider_400():
    owner = await _make_user(role="admin")
    tid = await _owned_task(owner)
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{tid}/image_model",
                          json={"actor": owner, "provider": "grok"})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_set_image_model_non_owner_404():
    owner = await _make_user(role="admin")
    other = await _make_user(role="C")
    tid = await _owned_task(owner)
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{tid}/image_model",
                          json={"actor": other, "provider": "gemini"})
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_set_image_model_wrong_status_400():
    owner = await _make_user(role="admin")
    tid = await _owned_task(owner, status="review")
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{tid}/image_model",
                          json={"actor": owner, "provider": "gemini"})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_asset_gen_uses_task_provider():
    """任务选了 gemini → asset_gen 每张图都以 provider='gemini' 调 generate_image；
    未选（NULL）→ provider=None（走全局默认 gpt-image-2）。"""
    fake = {"hash": "x", "image_url": "https://example.com/out.png",
            "model_version": "mock"}

    async def _run(provider):
        tid = await _owned_task(await _make_user(role="admin"),
                                status="confirm_gen", provider=provider)
        async with SessionLocal() as s:
            for i in range(1, 7):
                s.add(PageCopy(task_id=tid, page_index=i, body=f"第{i}页",
                               claim_ids=[]))
            await s.commit()
        with patch("src.config.settings.mock_image_gen", True), \
             patch("src.gateway.image_gen.generate_image",
                   new=AsyncMock(return_value=fake)) as gen:
            await node_asset_gen({"task_id": tid})
        return gen

    gen = await _run("gemini")
    assert gen.await_count == 6
    assert all(c.kwargs.get("provider") == "gemini" for c in gen.await_args_list)

    gen = await _run(None)
    assert gen.await_count == 6
    assert all(c.kwargs.get("provider") is None for c in gen.await_args_list)
