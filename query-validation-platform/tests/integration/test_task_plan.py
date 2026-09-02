"""视觉策划方案 API（2026-09-02，迁移 017）：

- GET  /api/tasks/{id}/plan   属主 200；非属主 404；未知用户 401
- PUT  /api/tasks/{id}/plan   人工编辑保存：仅 confirm_gen；格式校验 422
- POST /api/tasks/{id}/replan 按意见重策划：仅 confirm_gen；mock LLM 成功更新
"""
import uuid

import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, text

from src.api.auth import hash_password
from src.api.main import app
from src.db.session import SessionLocal
from src.models.tasks import Task

PLAN = {"pages": [
    {"page": i, "composition": f"构图{i}", "text_form": f"形式{i}",
     "palette": "暖调", "elements": f"元素{i}", "focus": f"焦点{i}",
     "title_zone": "top"} for i in range(1, 7)],
    "style": "治愈暖彩", "no_text": True}


def _uniq():
    return uuid.uuid4().hex[:8]


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _mk_user(role="C"):
    name = f"u-{_uniq()}"
    async with SessionLocal() as s:
        await s.execute(text(
            "INSERT INTO users (name, role, password_hash) VALUES (:n, :r, :p)"),
            {"n": name, "r": role, "p": hash_password("pw-123456")})
        await s.commit()
    return name


async def _mk_task(owner, status="confirm_gen", with_plan=True):
    async with SessionLocal() as s:
        uid = (await s.execute(text(
            "SELECT id FROM users WHERE name = :n"), {"n": owner})).scalar_one()
        task = Task(idempotency_key=f"k-{_uniq()}", query=f"q-{_uniq()}",
                    content_type="generic", mode="general", status=status,
                    created_by=uid, plan_json=PLAN if with_plan else None)
        s.add(task)
        await s.commit()
        return task.id


@pytest.mark.asyncio
async def test_get_plan_permissions():
    owner, other = await _mk_user(), await _mk_user()
    tid = await _mk_task(owner)
    async with _client() as ac:
        r = await ac.get(f"/api/tasks/{tid}/plan?actor={owner}")
        assert r.status_code == 200
        assert r.json()["plan"]["pages"][0]["composition"] == "构图1"
        assert r.json()["style"] is None or isinstance(r.json()["style"], str)
        r = await ac.get(f"/api/tasks/{tid}/plan?actor={other}")
        assert r.status_code == 404                       # 非属主不可见
        r = await ac.get(f"/api/tasks/{tid}/plan?actor=ghost-{_uniq()}")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_put_plan_validates_and_saves():
    owner = await _mk_user()
    tid = await _mk_task(owner)
    async with _client() as ac:
        # 5 页 → 422
        bad = {"pages": PLAN["pages"][:5]}
        r = await ac.put(f"/api/tasks/{tid}/plan",
                         json={"actor": owner, "plan": bad})
        assert r.status_code == 422
        # 合法编辑：改第 2 页构图
        pages = [dict(p) for p in PLAN["pages"]]
        pages[1]["composition"] = "人工改过的构图"
        r = await ac.put(f"/api/tasks/{tid}/plan",
                         json={"actor": owner, "plan": {"pages": pages}})
        assert r.status_code == 200, r.text
        body = r.json()["plan"]
        assert body["pages"][1]["composition"] == "人工改过的构图"
        assert body["model"] == "human_edit"
    # 非 confirm_gen 状态不可编辑
    tid2 = await _mk_task(owner, status="review")
    async with _client() as ac:
        r = await ac.put(f"/api/tasks/{tid2}/plan",
                         json={"actor": owner, "plan": PLAN})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_replan_success_and_guards():
    owner = await _mk_user()
    tid = await _mk_task(owner)
    new_plan = {"pages": [dict(p, composition="新构图") for p in PLAN["pages"]],
                "model": "mock", "cost_cny": 0.0}
    # 端点内 lazy import generate_plan（from ... import 读取模块当前属性），
    # patch 服务模块属性即生效
    with patch("src.services.art_director.generate_plan",
               new=AsyncMock(return_value=new_plan)):
        async with _client() as ac:
            r = await ac.post(f"/api/tasks/{tid}/replan",
                              json={"actor": owner, "feedback": "封面太挤"})
            assert r.status_code == 200, r.text
            assert r.json()["plan"]["pages"][0]["composition"] == "新构图"
    # 状态守卫：review 状态不可重策划
    tid2 = await _mk_task(owner, status="review")
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{tid2}/replan",
                          json={"actor": owner, "feedback": ""})
        assert r.status_code == 400
