"""创建者自助修正（2026-08-27）：POST /api/tasks/{id}/fix。

属主（或 admin）对终态任务做定点标记 → 写 reject_marks(role="creator") →
自动 enqueue_regen 走 partial_regen。权限按归属隔离严格校验。
"""
import uuid

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, text

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


GOOD_MARKS = [{"item_type": "page", "page_index": 2, "reason": "文案事实有误"},
              {"item_type": "image", "page_index": 3, "reason": "配图与主题不符"}]


@pytest.mark.asyncio
async def test_owner_fix_creates_marks_and_enqueues_partial_regen():
    owner = await _make_user()
    task = await _make_task(status="review", owner=await _uid(owner))
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{task.id}/fix",
                          json={"actor": owner, "marks": GOOD_MARKS})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["kind"] == "partial_regen" and body["mark_count"] == 2
    async with SessionLocal() as s:
        marks = (await s.execute(select(RejectMark).where(
            RejectMark.task_id == task.id))).scalars().all()
        assert len(marks) == 2
        assert all(m.role == "creator" and m.status == "open" for m in marks)
        t = (await s.execute(select(Task).where(Task.id == task.id))).scalar_one()
        assert t.status == "draft"  # enqueue_regen 置回生产态


@pytest.mark.asyncio
async def test_fix_approved_task_also_allowed():
    owner = await _make_user()
    task = await _make_task(status="approved", owner=await _uid(owner))
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{task.id}/fix",
                          json={"actor": owner,
                                "marks": [GOOD_MARKS[0]]})
        assert r.status_code == 200 and r.json()["mark_count"] == 1


@pytest.mark.asyncio
async def test_non_owner_cannot_fix():
    owner = await _make_user()
    other = await _make_user()
    task = await _make_task(status="review", owner=await _uid(owner))
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{task.id}/fix",
                          json={"actor": other, "marks": GOOD_MARKS})
        assert r.status_code == 404  # 不泄露任务是否存在
    async with SessionLocal() as s:
        n = len((await s.execute(select(RejectMark).where(
            RejectMark.task_id == task.id))).scalars().all())
        assert n == 0


@pytest.mark.asyncio
async def test_admin_can_fix_any_task():
    owner = await _make_user()
    admin = await _make_user(role="admin")
    task = await _make_task(status="review", owner=await _uid(owner))
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{task.id}/fix",
                          json={"actor": admin, "marks": [GOOD_MARKS[1]]})
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_fix_rejects_running_or_draft_task():
    owner = await _make_user()
    uid = await _uid(owner)
    for status in ("draft", "failed"):
        task = await _make_task(status=status, owner=uid)
        async with _client() as ac:
            r = await ac.post(f"/api/tasks/{task.id}/fix",
                              json={"actor": owner, "marks": GOOD_MARKS})
            assert r.status_code == 400, f"status={status} should be rejected"


@pytest.mark.asyncio
async def test_fix_mark_validation():
    owner = await _make_user()
    uid = await _uid(owner)
    async with _client() as ac:
        # 空标记
        task = await _make_task(owner=uid)
        r = await ac.post(f"/api/tasks/{task.id}/fix",
                          json={"actor": owner, "marks": []})
        assert r.status_code == 400
        # 缺问题说明
        task = await _make_task(owner=uid)
        r = await ac.post(f"/api/tasks/{task.id}/fix",
                          json={"actor": owner,
                                "marks": [{"item_type": "page", "page_index": 1,
                                           "reason": "  "}]})
        assert r.status_code == 400
        # 非法页码 / 类型
        task = await _make_task(owner=uid)
        r = await ac.post(f"/api/tasks/{task.id}/fix",
                          json={"actor": owner,
                                "marks": [{"item_type": "image", "page_index": 7,
                                           "reason": "x"}]})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_fix_unknown_actor_401():
    owner = await _make_user()
    task = await _make_task(status="review", owner=await _uid(owner))
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{task.id}/fix",
                          json={"actor": f"ghost-{_uniq()}", "marks": GOOD_MARKS})
        assert r.status_code == 401
