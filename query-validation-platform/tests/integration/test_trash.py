"""回收站：软删除/恢复/彻底删除 + 列表隐藏 + 权限 + 导出后自动入站。"""
import asyncio
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


@pytest.mark.asyncio
async def test_export_approved_auto_trashes_exported_tasks():
    """导出已通过内容包成功后，已打包任务自动移入回收站（可恢复）。"""
    admin = await _make_user(role="admin")
    t1 = await _make_task(status="approved")
    t2 = await _make_task(status="approved")
    keep = await _make_task(status="review")  # 未通过任务不受影响
    async with SessionLocal() as s:
        s.add(Draft(task_id=t1.id, version=1, body="正文1",
                    model_version="m", prompt_version="p"))
        s.add(Draft(task_id=t2.id, version=1, body="正文2",
                    model_version="m", prompt_version="p"))
        await s.commit()
    async with _client() as ac:
        r = await ac.post(f"/api/export/approved/start?actor={admin}")
        assert r.status_code == 200 and r.json()["total"] == 2
        job_id = r.json()["job_id"]
        st = {}
        for _ in range(50):
            st = (await ac.get(f"/api/export/{job_id}")).json()
            if st["status"] in ("done", "error"):
                break
            await asyncio.sleep(0.1)
        assert st["status"] == "done", st
        assert "回收站" in st["detail"]
        # 已导出的两条进入回收站，prev_status=approved
        r = await ac.get("/api/trash")
        bin_items = {t["id"]: t for t in r.json()["items"]}
        assert str(t1.id) in bin_items and str(t2.id) in bin_items
        assert bin_items[str(t1.id)]["prev_status"] == "approved"
        # 任务列表只剩未通过的那条
        r = await ac.get("/api/tasks")
        ids = [t["id"] for t in r.json()["items"]]
        assert str(keep.id) in ids and str(t1.id) not in ids
        # 已落盘的 zip 仍可下载（入站不影响下载窗口）
        d = await ac.get(f"/api/export/{job_id}/download/1")
        assert d.status_code == 200
        # 恢复后回到 approved，重新出现在任务列表
        r = await ac.post(f"/api/tasks/{t1.id}/restore?actor={admin}")
        assert r.json()["status"] == "approved"


@pytest.mark.asyncio
async def test_trash_batch_and_restore_batch():
    """批量移入：终态移入、非终态跳过；批量恢复：还原各自移入前状态。"""
    user = await _make_user()
    t_review = await _make_task(status="review")
    t_failed = await _make_task(status="failed")
    t_draft = await _make_task(status="draft")       # 不可移入
    ids = [str(t_review.id), str(t_failed.id), str(t_draft.id)]
    async with _client() as ac:
        r = await ac.post("/api/tasks/trash_batch",
                          json={"task_ids": ids, "actor": user})
        assert r.json() == {"ok": True, "moved": 2, "skipped": 1}
        # 幂等：再移一次全部跳过
        r = await ac.post("/api/tasks/trash_batch",
                          json={"task_ids": ids, "actor": user})
        assert r.json()["moved"] == 0 and r.json()["skipped"] == 3
        # 回收站有 2 条
        r = await ac.get("/api/trash")
        assert r.json()["total"] == 2
        # 批量恢复
        r = await ac.post("/api/tasks/restore_batch",
                          json={"task_ids": ids, "actor": user})
        assert r.json()["restored"] == 2 and r.json()["skipped"] == 1
    async with SessionLocal() as s:
        t1 = (await s.execute(select(Task).where(Task.id == t_review.id))).scalar_one()
        t2 = (await s.execute(select(Task).where(Task.id == t_failed.id))).scalar_one()
        t3 = (await s.execute(select(Task).where(Task.id == t_draft.id))).scalar_one()
        assert t1.status == "review" and t1.prev_status is None
        assert t2.status == "failed"
        assert t3.status == "draft"


@pytest.mark.asyncio
async def test_batch_requires_valid_actor_and_nonempty_ids():
    user = await _make_user()
    task = await _make_task(status="review")
    async with _client() as ac:
        r = await ac.post("/api/tasks/trash_batch",
                          json={"task_ids": [str(task.id)], "actor": f"ghost-{_uniq()}"})
        assert r.status_code == 401
        r = await ac.post("/api/tasks/trash_batch",
                          json={"task_ids": ["not-a-uuid"], "actor": user})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_trash_list_sorting():
    """回收站列表排序：sort 白名单 trashed_at/prev_status/mode/trashed_by + order，非法回退默认。"""
    user = await _make_user()
    t1 = await _make_task(status="review", query=f"q-a-{_uniq()}")   # mode=general
    t2 = await _make_task(status="failed", query=f"q-b-{_uniq()}")
    async with _client() as ac:
        # t1 先入站，t2 后入站（trashed_at 有先后）
        await ac.post(f"/api/tasks/{t1.id}/trash?actor={user}")
        await ac.post(f"/api/tasks/{t2.id}/trash?actor={user}")
        r = await ac.get("/api/trash")  # 默认移入时间倒序
        assert [t["query"] for t in r.json()["items"]] == [t2.query, t1.query]
        r = await ac.get("/api/trash?sort=trashed_at&order=asc")
        assert [t["query"] for t in r.json()["items"]] == [t1.query, t2.query]
        r = await ac.get("/api/trash?sort=prev_status&order=asc")  # failed < review
        assert [t["query"] for t in r.json()["items"]] == [t2.query, t1.query]
        r = await ac.get("/api/trash?sort=bogus&order=sideways")   # 非法回退默认
        assert [t["query"] for t in r.json()["items"]] == [t2.query, t1.query]


@pytest.mark.asyncio
async def test_purge_non_admin_needs_real_admin_password():
    """非 admin 彻底删除：无密码/错密码 403；正确的在职 admin 密码放行（服务端校验）。"""
    await _make_user(role="admin", pw="admin-pass-1")
    user = await _make_user()
    task = await _make_task(status="rejected")
    tid = str(task.id)
    async with _client() as ac:
        await ac.post(f"/api/tasks/{tid}/trash?actor={user}")
        r = await ac.delete(f"/api/tasks/{tid}/purge?actor={user}")
        assert r.status_code == 403
        r = await ac.delete(f"/api/tasks/{tid}/purge?actor={user}&admin_password=wrong-pw")
        assert r.status_code == 403
        r = await ac.delete(f"/api/tasks/{tid}/purge?actor={user}&admin_password=admin-pass-1")
        assert r.status_code == 200 and r.json()["ok"] is True
    async with SessionLocal() as s:
        assert (await s.execute(
            select(func.count(Task.id)).where(Task.id == task.id))).scalar() == 0
