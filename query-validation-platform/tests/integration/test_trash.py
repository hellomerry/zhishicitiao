"""回收站：软删除/恢复/彻底删除 + 列表隐藏 + 权限。"""
import uuid

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import func, select, text

from src.api.auth import hash_password
from src.api.main import app
from src.db.session import SessionLocal
from src.models.drafts import Draft
from src.models.tasks import Task


def _uniq() -> str:
    return uuid.uuid4().hex[:8]


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _make_user(name=None, role="A", pw="pw-123456"):
    name = name or f"u-{_uniq()}"
    async with SessionLocal() as s:
        await s.execute(text(
            "INSERT INTO users (name, role, password_hash) VALUES (:n, :r, :p)"),
            {"n": name, "r": role, "p": hash_password(pw)})
        await s.commit()
    return name


async def _make_task(status="review", query=None) -> Task:
    async with SessionLocal() as session:
        task = Task(idempotency_key=f"k-{_uniq()}", query=query or f"q-{_uniq()}",
                    content_type="generic", mode="general", status=status)
        session.add(task)
        await session.commit()
        return task


@pytest.mark.asyncio
async def test_trash_hides_from_task_list_and_shows_in_bin():
    user = await _make_user()
    task = await _make_task(status="review")
    tid = str(task.id)
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{tid}/trash?actor={user}")
        assert r.status_code == 200 and r.json()["prev_status"] == "review"
        # 任务列表默认不含回收站任务
        r = await ac.get("/api/tasks")
        assert tid not in [t["id"] for t in r.json()["items"]]
        # 回收站可见
        r = await ac.get("/api/trash")
        item = [t for t in r.json()["items"] if t["id"] == tid][0]
        assert item["prev_status"] == "review" and item["trashed_by"] == user
        # 审计日志
        r = await ac.get(f"/api/activity?actor={user}&action=trash_task")
        assert r.json()["total"] == 1
    async with SessionLocal() as s:
        t = (await s.execute(select(Task).where(Task.id == task.id))).scalar_one()
        assert t.status == "trashed" and t.prev_status == "review"
        assert t.trashed_at is not None


@pytest.mark.asyncio
async def test_trash_rejects_non_terminal_status():
    user = await _make_user()
    for status in ("draft", "processing"):
        task = await _make_task(status=status)
        async with _client() as ac:
            r = await ac.post(f"/api/tasks/{task.id}/trash?actor={user}")
            assert r.status_code == 409


@pytest.mark.asyncio
async def test_trash_unknown_actor_401_and_idempotent():
    task = await _make_task(status="failed")
    tid = str(task.id)
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{tid}/trash?actor=ghost-{_uniq()}")
        assert r.status_code == 401
        user = await _make_user()
        await ac.post(f"/api/tasks/{tid}/trash?actor={user}")
        # 重复移入幂等
        r = await ac.post(f"/api/tasks/{tid}/trash?actor={user}")
        assert r.json().get("already") is True


@pytest.mark.asyncio
async def test_restore_returns_to_prev_status():
    user = await _make_user()
    task = await _make_task(status="approved")
    tid = str(task.id)
    async with _client() as ac:
        await ac.post(f"/api/tasks/{tid}/trash?actor={user}")
        r = await ac.post(f"/api/tasks/{tid}/restore?actor={user}")
        assert r.json()["status"] == "approved"
        # 恢复后重新出现在任务列表
        r = await ac.get("/api/tasks")
        assert tid in [t["id"] for t in r.json()["items"]]
        # 不在回收站的任务不能恢复
        r = await ac.post(f"/api/tasks/{tid}/restore?actor={user}")
        assert r.status_code == 409
    async with SessionLocal() as s:
        t = (await s.execute(select(Task).where(Task.id == task.id))).scalar_one()
        assert t.status == "approved" and t.prev_status is None and t.trashed_at is None


@pytest.mark.asyncio
async def test_purge_requires_admin_and_trashed():
    admin = await _make_user(role="admin")
    user = await _make_user()
    task = await _make_task(status="rejected")
    tid = str(task.id)
    async with _client() as ac:
        # 未入站不能彻底删除
        r = await ac.delete(f"/api/tasks/{tid}/purge?actor={admin}")
        assert r.status_code == 409
        await ac.post(f"/api/tasks/{tid}/trash?actor={user}")
        # 非 admin 不能彻底删除
        r = await ac.delete(f"/api/tasks/{tid}/purge?actor={user}")
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_purge_admin_deletes_task_and_content():
    admin = await _make_user(role="admin")
    user = await _make_user()
    task = await _make_task(status="review")
    tid = str(task.id)
    async with SessionLocal() as s:
        s.add(Draft(task_id=task.id, version=1, body="正文",
                    model_version="m", prompt_version="p"))
        await s.commit()
    async with _client() as ac:
        await ac.post(f"/api/tasks/{tid}/trash?actor={user}")
        r = await ac.delete(f"/api/tasks/{tid}/purge?actor={admin}")
        assert r.status_code == 200 and r.json()["ok"] is True
    async with SessionLocal() as s:
        assert (await s.execute(
            select(func.count(Task.id)).where(Task.id == task.id))).scalar() == 0
        assert (await s.execute(
            select(func.count(Draft.id)).where(Draft.task_id == task.id))).scalar() == 0
    async with _client() as ac:
        r = await ac.get(f"/api/activity?actor={admin}&action=purge_task")
        assert r.json()["total"] == 1
