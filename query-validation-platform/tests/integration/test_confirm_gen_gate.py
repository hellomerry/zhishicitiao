"""生图前人工确认门（2026-08-27）：pipeline 跑到 page_split 停（confirm_gen），
人工确认后 start_gen 以 gen_resume 续跑全链（已完成节点幂等跳过）。"""
import uuid

import pytest
from unittest.mock import patch
from httpx import AsyncClient, ASGITransport
from sqlalchemy import func, select, text

from src.api.auth import hash_password
from src.api.main import app
from src.db.session import SessionLocal
from src.models.assets import Asset
from src.models.drafts import PageCopy
from src.models.tasks import Task
from src.stream.scheduler import scheduler

FAKE_DRAFT = {
    "text": "这是测试正文。" * 100,
    "model_version": "deepseek/deepseek-chat",
    "cost_cny": 0.01,
    "degraded": False,
}


def _uniq() -> str:
    return uuid.uuid4().hex[:8]


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _mk_task(status="draft") -> Task:
    async with SessionLocal() as session:
        task = Task(idempotency_key=f"k-{_uniq()}", query=f"q-{_uniq()}",
                    content_type="generic", mode="general", status=status)
        session.add(task)
        await session.commit()
        return task


async def _mk_user(role="C"):
    name = f"u-{_uniq()}"
    async with SessionLocal() as s:
        await s.execute(text(
            "INSERT INTO users (name, role, password_hash) VALUES (:n, :r, :p)"),
            {"n": name, "r": role, "p": hash_password("pw-123456")})
        await s.commit()
    return name


async def _own(task_id, user):
    async with SessionLocal() as s:
        uid = (await s.execute(text(
            "SELECT id FROM users WHERE name = :n"), {"n": user})).scalar_one()
        await s.execute(text("UPDATE tasks SET created_by = :u WHERE id = :t"),
                        {"u": uid, "t": task_id})
        await s.commit()


async def _status(task_id):
    async with SessionLocal() as s:
        return (await s.execute(select(Task).where(Task.id == task_id))).scalar_one().status


@pytest.mark.asyncio
async def test_pipeline_stops_at_gate_with_content_ready():
    """pipeline（导入路径）跑到 page_split 停：状态 confirm_gen，
    分页文案就绪、AI 配图未生成（asset_gen 未执行）。"""
    task = await _mk_task()
    with patch("src.pipeline.nodes.call_with_failover", return_value=FAKE_DRAFT):
        await scheduler._process(task.id, kind="pipeline")
    assert await _status(task.id) == "confirm_gen"
    async with SessionLocal() as s:
        pages = (await s.execute(select(func.count(PageCopy.id)).where(
            PageCopy.task_id == task.id))).scalar()
        assert pages == 6
        ai = (await s.execute(select(func.count(Asset.id)).where(
            Asset.task_id == task.id,
            Asset.source_type == "ai_generated"))).scalar()
        assert ai == 0


@pytest.mark.asyncio
async def test_gen_resume_completes_pipeline():
    """过门后 gen_resume 续跑：前半段节点幂等跳过，生图/校验执行，状态 review。"""
    task = await _mk_task()
    with patch("src.pipeline.nodes.call_with_failover", return_value=FAKE_DRAFT):
        await scheduler._process(task.id, kind="pipeline")
        assert await _status(task.id) == "confirm_gen"
        await scheduler._process(task.id, kind="gen_resume")
    assert await _status(task.id) == "review"
    async with SessionLocal() as s:
        ai = (await s.execute(select(func.count(Asset.id)).where(
            Asset.task_id == task.id,
            Asset.source_type == "ai_generated"))).scalar()
        assert ai == 6


@pytest.mark.asyncio
async def test_gate_disabled_runs_full_pipeline(monkeypatch):
    """IMAGE_GEN_CONFIRM_GATE=false 时保持旧行为：一把跑完直接 review。"""
    monkeypatch.setattr("src.stream.scheduler.settings.image_gen_confirm_gate", False)
    task = await _mk_task()
    with patch("src.pipeline.nodes.call_with_failover", return_value=FAKE_DRAFT):
        await scheduler._process(task.id, kind="pipeline")
    assert await _status(task.id) == "review"


@pytest.mark.asyncio
async def test_start_gen_api_owner_ok_and_guards():
    """start_gen：属主放行 200 并回队列（gen_resume）；非属主 404、状态不对 400、
    未知用户 401。"""
    owner = await _mk_user()
    other = await _mk_user()
    task = await _mk_task(status="confirm_gen")
    await _own(task.id, owner)
    async with _client() as ac:
        # 非属主
        r = await ac.post(f"/api/tasks/{task.id}/start_gen?actor={other}")
        assert r.status_code == 404
        # 未知用户
        r = await ac.post(f"/api/tasks/{task.id}/start_gen?actor=ghost-{_uniq()}")
        assert r.status_code == 401
        # 属主放行
        r = await ac.post(f"/api/tasks/{task.id}/start_gen?actor={owner}")
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "gen_resume"
    assert await _status(task.id) == "draft"
    # 已回队列的任务不可重复放行
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{task.id}/start_gen?actor={owner}")
        assert r.status_code == 400
