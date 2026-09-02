"""版式永久禁用（2026-09-02，迁移 019）：POST /api/tasks/{id}/layout_ban。

用户对某页合成文字版式不满意 → 永久禁用（tasks.layout_bans）→ 该页以后
所有重生成经 slot_for_page 顺延到下一个未禁用版式；默认立即自动重出该页配图
（写 creator 标记 + enqueue_regen，与 fix 同链路）。权限与 fix 一致。
"""
import uuid

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, text
from unittest.mock import patch, AsyncMock

from src.api.auth import hash_password
from src.api.main import app
from src.db.session import SessionLocal
from src.models.review import RejectMark
from src.models.tasks import Task


def _uniq() -> str:
    return uuid.uuid4().hex[:8]


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _make_user(role="C"):
    name = f"u-{_uniq()}"
    async with SessionLocal() as s:
        await s.execute(text(
            "INSERT INTO users (name, role, password_hash) VALUES (:n, :r, :p)"),
            {"n": name, "r": role, "p": hash_password("pw-123456")})
        await s.commit()
    return name


async def _uid(name):
    async with SessionLocal() as s:
        return (await s.execute(
            text("SELECT id FROM users WHERE name = :n"), {"n": name})).scalar_one()


async def _make_task(status="review", owner=None) -> Task:
    async with SessionLocal() as session:
        task = Task(idempotency_key=f"k-{_uniq()}", query=f"q-{_uniq()}",
                    content_type="generic", mode="general", status=status,
                    created_by=owner)
        session.add(task)
        await session.commit()
        return task


@pytest.mark.asyncio
async def test_ban_layout_persists_and_enqueues_regen():
    owner = await _make_user()
    task = await _make_task(owner=await _uid(owner))
    with patch("src.stream.scheduler.scheduler.enqueue", new=AsyncMock(return_value=True)):
        async with _client() as ac:
            r = await ac.post(f"/api/tasks/{task.id}/layout_ban",
                              json={"actor": owner, "page_index": 6, "slot": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["banned"] == [2] and body["new_slot"] != 2
    assert body["regen"] == "partial_regen"
    async with SessionLocal() as s:
        t = (await s.execute(select(Task).where(Task.id == task.id))).scalar_one()
        assert (t.layout_bans or {}).get("6") == [2]
        # 自动重出：落了 creator 定点标记（fix 同链路）
        marks = (await s.execute(select(RejectMark).where(
            RejectMark.task_id == task.id))).scalars().all()
        assert len(marks) == 1 and marks[0].role == "creator" \
            and marks[0].page_index == 6 and marks[0].status == "open"
        assert "永久禁用" in marks[0].reason


@pytest.mark.asyncio
async def test_ban_layout_accumulates_and_caps_at_five():
    owner = await _make_user()
    task = await _make_task(owner=await _uid(owner))
    with patch("src.stream.scheduler.scheduler.enqueue", new=AsyncMock(return_value=True)):
        async with _client() as ac:
            for slot in (2, 3, 4, 5, 6):
                r = await ac.post(f"/api/tasks/{task.id}/layout_ban",
                                  json={"actor": owner, "page_index": 6,
                                        "slot": slot, "regen": False})
                assert r.status_code == 200, r.text
            # 第 6 个被拒：每页至少保留 1 种可用版式
            r = await ac.post(f"/api/tasks/{task.id}/layout_ban",
                              json={"actor": owner, "page_index": 6,
                                    "slot": 1, "regen": False})
            assert r.status_code == 400
            # 重复禁用同一槽位 → 400
            r = await ac.post(f"/api/tasks/{task.id}/layout_ban",
                              json={"actor": owner, "page_index": 6,
                                    "slot": 2, "regen": False})
            assert r.status_code == 400


@pytest.mark.asyncio
async def test_ban_layout_ownership_and_validation():
    owner = await _make_user()
    other = await _make_user()
    task = await _make_task(owner=await _uid(owner))
    async with _client() as ac:
        # 非属主非 admin → 404（归属隔离）
        r = await ac.post(f"/api/tasks/{task.id}/layout_ban",
                          json={"actor": other, "page_index": 6, "slot": 2})
        assert r.status_code == 404
        # 非法参数
        r = await ac.post(f"/api/tasks/{task.id}/layout_ban",
                          json={"actor": owner, "page_index": 7, "slot": 2})
        assert r.status_code == 400
        r = await ac.post(f"/api/tasks/{task.id}/layout_ban",
                          json={"actor": owner, "page_index": 6, "slot": 0})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_ban_layout_rejects_running_task():
    owner = await _make_user()
    task = await _make_task(status="processing", owner=await _uid(owner))
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{task.id}/layout_ban",
                          json={"actor": owner, "page_index": 6, "slot": 2})
        assert r.status_code == 400
