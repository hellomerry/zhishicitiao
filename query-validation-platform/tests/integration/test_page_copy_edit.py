"""分页文案手动编辑接口（2026-09-01）：弹窗改文案落库 + 权限/状态/参数校验。"""
import uuid
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, text as _text
from src.api.main import app
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.models.drafts import PageCopy


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _make_user(role="C"):
    from src.api.auth import hash_password
    name = f"u-{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as s:
        await s.execute(_text(
            "INSERT INTO users (name, role, password_hash) VALUES (:n, :r, :p)"),
            {"n": name, "r": role, "p": hash_password("pw-123456")})
        await s.commit()
    return name


async def _owned_task(owner_name, status="review", with_copy=True):
    async with SessionLocal() as s:
        task = Task(idempotency_key=f"pc-{uuid.uuid4().hex[:8]}", query="测试词条",
                    content_type="general", mode="general", status=status)
        s.add(task)
        await s.commit()
        await s.refresh(task)
        tid = task.id
        uid = (await s.execute(_text(
            "SELECT id FROM users WHERE name = :n"), {"n": owner_name})).scalar_one()
        await s.execute(_text(
            "UPDATE tasks SET created_by = :u WHERE id = :t"), {"u": uid, "t": tid})
        if with_copy:
            s.add(PageCopy(task_id=tid, page_index=2, body="原文案", claim_ids=[]))
        await s.commit()
    return tid


@pytest.mark.asyncio
async def test_edit_page_copy_ok():
    owner = await _make_user()
    tid = await _owned_task(owner)
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{tid}/page_copy",
                          json={"actor": owner, "page_index": 2, "body": "改后的新文案"})
    assert r.status_code == 200, r.text
    assert r.json()["body"] == "改后的新文案"
    async with SessionLocal() as s:
        row = (await s.execute(select(PageCopy).where(
            PageCopy.task_id == tid, PageCopy.page_index == 2))).scalar_one()
        assert row.body == "改后的新文案"


@pytest.mark.asyncio
async def test_edit_page_copy_creates_missing_row():
    owner = await _make_user()
    tid = await _owned_task(owner, with_copy=False)
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{tid}/page_copy",
                          json={"actor": owner, "page_index": 4, "body": "补写的第4页"})
    assert r.status_code == 200, r.text
    async with SessionLocal() as s:
        row = (await s.execute(select(PageCopy).where(
            PageCopy.task_id == tid, PageCopy.page_index == 4))).scalar_one()
        assert row.body == "补写的第4页"


@pytest.mark.asyncio
async def test_edit_page_copy_non_owner_404():
    owner = await _make_user()
    other = await _make_user()
    tid = await _owned_task(owner)
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{tid}/page_copy",
                          json={"actor": other, "page_index": 2, "body": "越权修改"})
    assert r.status_code == 404
    async with SessionLocal() as s:
        row = (await s.execute(select(PageCopy).where(
            PageCopy.task_id == tid, PageCopy.page_index == 2))).scalar_one()
        assert row.body == "原文案"  # 未被改动


@pytest.mark.asyncio
async def test_edit_page_copy_admin_allowed():
    owner = await _make_user()
    admin = await _make_user(role="admin")
    tid = await _owned_task(owner)
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{tid}/page_copy",
                          json={"actor": admin, "page_index": 2, "body": "管理员代改"})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_edit_page_copy_rejects_non_terminal_status():
    owner = await _make_user()
    tid = await _owned_task(owner, status="processing")
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{tid}/page_copy",
                          json={"actor": owner, "page_index": 2, "body": "生产中改"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_edit_page_copy_validates_input():
    owner = await _make_user()
    tid = await _owned_task(owner)
    async with _client() as ac:
        # 空文案
        r = await ac.post(f"/api/tasks/{tid}/page_copy",
                          json={"actor": owner, "page_index": 2, "body": "   "})
        assert r.status_code == 400
        # 超长（>200 字）
        r = await ac.post(f"/api/tasks/{tid}/page_copy",
                          json={"actor": owner, "page_index": 2, "body": "长" * 201})
        assert r.status_code == 400
        # 页码越界
        r = await ac.post(f"/api/tasks/{tid}/page_copy",
                          json={"actor": owner, "page_index": 7, "body": "越界"})
        assert r.status_code == 400
    async with SessionLocal() as s:
        row = (await s.execute(select(PageCopy).where(
            PageCopy.task_id == tid, PageCopy.page_index == 2))).scalar_one()
        assert row.body == "原文案"  # 均未被改动
