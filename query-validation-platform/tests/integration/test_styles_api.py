"""风格关键词库 API（/api/styles）：CRUD / CSV 导入 / 模板下载 +
用户隔离（迁移 012：个人/公共、越权 403/404）/ 偏好统计 / 默认风格。"""
import uuid

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import text

from src.api.main import app
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.models.assets import Asset


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _uniq() -> str:
    return uuid.uuid4().hex[:8]


async def _make_user(role="A") -> str:
    """建一个在职用户，返回用户名。"""
    name = f"u-{_uniq()}"
    async with SessionLocal() as s:
        await s.execute(text("INSERT INTO users (name, role) VALUES (:n, :r)"),
                        {"n": name, "r": role})
        await s.commit()
    return name


# ---------- CRUD + 用户隔离 ----------

@pytest.mark.asyncio
async def test_style_crud_flow_personal():
    user = await _make_user()
    async with _client() as c:
        # 新增（默认写入个人库）
        r = await c.post(f"/api/styles?actor={user}", json={
            "style_name": "科技蓝调", "keywords": "手机,数码", "description": "深蓝科技光感"})
        assert r.status_code == 200 and r.json()["ok"]
        # 列表：我的条目，标注 scope/owner
        r = await c.get(f"/api/styles?actor={user}")
        items = r.json()["items"]
        assert len(items) == 1 and items[0]["style_name"] == "科技蓝调"
        assert items[0]["scope"] == "mine" and items[0]["owner_name"] == user
        assert items[0]["enabled"] is True
        sid = items[0]["id"]
        # 同名覆盖更新（含停用）
        r = await c.post(f"/api/styles?actor={user}", json={
            "style_name": "科技蓝调", "keywords": "芯片", "description": "新描述",
            "enabled": False})
        assert r.status_code == 200
        items = (await c.get(f"/api/styles?actor={user}")).json()["items"]
        assert len(items) == 1
        assert items[0]["keywords"] == "芯片" and items[0]["enabled"] is False
        # 删除
        r = await c.delete(f"/api/styles/{sid}?actor={user}")
        assert r.status_code == 200
        assert (await c.get(f"/api/styles?actor={user}")).json()["items"] == []
        # 再删 → 404；坏 id → 400
        assert (await c.delete(f"/api/styles/{sid}?actor={user}")).status_code == 404
        assert (await c.delete(f"/api/styles/not-a-uuid?actor={user}")).status_code == 400


@pytest.mark.asyncio
async def test_style_requires_known_actor():
    async with _client() as c:
        assert (await c.get("/api/styles")).status_code == 401
        assert (await c.get("/api/styles?actor=nobody")).status_code == 401


@pytest.mark.asyncio
async def test_style_name_required():
    user = await _make_user()
    async with _client() as c:
        r = await c.post(f"/api/styles?actor={user}", json={"style_name": "  "})
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_public_library_admin_only():
    admin = await _make_user(role="admin")
    user = await _make_user()
    async with _client() as c:
        # 普通用户写公共库 → 403
        r = await c.post(f"/api/styles?actor={user}", json={
            "style_name": "公共风", "public": True})
        assert r.status_code == 403
        # admin 写公共库 → 200，owner 为 NULL
        r = await c.post(f"/api/styles?actor={admin}", json={
            "style_name": "公共风", "keywords": "手机", "public": True})
        assert r.status_code == 200
        items = (await c.get(f"/api/styles?actor={user}")).json()["items"]
        pub = next(i for i in items if i["style_name"] == "公共风")
        assert pub["scope"] == "public" and pub["owner_id"] is None
        # 普通用户删公共条目 → 403；admin 删 → 200
        assert (await c.delete(f"/api/styles/{pub['id']}?actor={user}")).status_code == 403
        assert (await c.delete(f"/api/styles/{pub['id']}?actor={admin}")).status_code == 200


@pytest.mark.asyncio
async def test_cannot_touch_others_personal_style():
    u1 = await _make_user()
    u2 = await _make_user()
    async with _client() as c:
        await c.post(f"/api/styles?actor={u1}", json={"style_name": "私人风"})
        # u2 看不到 u1 的个人条目
        assert (await c.get(f"/api/styles?actor={u2}")).json()["items"] == []
        items = (await c.get(f"/api/styles?actor={u1}")).json()["items"]
        sid = items[0]["id"]
        # u2 删 u1 的条目 → 403
        assert (await c.delete(f"/api/styles/{sid}?actor={u2}")).status_code == 403


@pytest.mark.asyncio
async def test_same_name_coexists_in_personal_and_public():
    admin = await _make_user(role="admin")
    async with _client() as c:
        await c.post(f"/api/styles?actor={admin}", json={
            "style_name": "同名风", "description": "公共版", "public": True})
        await c.post(f"/api/styles?actor={admin}", json={
            "style_name": "同名风", "description": "个人版"})
        items = (await c.get(f"/api/styles?actor={admin}")).json()["items"]
        assert len(items) == 2
        assert {i["description"] for i in items} == {"公共版", "个人版"}


# ---------- CSV 导入 ----------

@pytest.mark.asyncio
async def test_style_csv_import_scope_and_idempotent_upsert():
    admin = await _make_user(role="admin")
    user = await _make_user()
    csv_text = ("style_name,keywords,description\n"
                "科技蓝调,手机,深蓝科技光感\n"
                "暖木家居,\"家具,装修\",暖木色系\n"
                ",无效行,缺风格名\n")
    async with _client() as c:
        # 普通用户导入 → 个人库
        r = await c.post("/api/styles/import",
                         files={"file": ("s.csv", csv_text.encode("utf-8"), "text/csv")},
                         data={"actor": user})
        body = r.json()
        assert body["imported"] == 2 and len(body["errors"]) == 1
        items = (await c.get(f"/api/styles?actor={user}")).json()["items"]
        assert all(i["scope"] == "mine" for i in items)
        # 同名再导入 = 覆盖更新，不新增
        r = await c.post("/api/styles/import",
                         files={"file": ("s.csv", "style_name,keywords,description\n科技蓝调,芯片,新描述\n".encode("utf-8"), "text/csv")},
                         data={"actor": user})
        assert r.json()["imported"] == 1
        items = (await c.get(f"/api/styles?actor={user}")).json()["items"]
        assert len(items) == 2
        tech = next(i for i in items if i["style_name"] == "科技蓝调")
        assert tech["keywords"] == "芯片" and tech["description"] == "新描述"
        # 普通用户导入公共库 → 403；admin 导入公共库 → 公共条目
        r = await c.post("/api/styles/import",
                         files={"file": ("s.csv", csv_text.encode("utf-8"), "text/csv")},
                         data={"actor": user, "public": "true"})
        assert r.status_code == 403
        r = await c.post("/api/styles/import",
                         files={"file": ("s.csv", "style_name,keywords,description\n公共导入风,关键词,描述\n".encode("utf-8"), "text/csv")},
                         data={"actor": admin, "public": "true"})
        assert r.json()["imported"] == 1
        items = (await c.get(f"/api/styles?actor={user}")).json()["items"]
        pub = next(i for i in items if i["style_name"] == "公共导入风")
        assert pub["scope"] == "public"


@pytest.mark.asyncio
async def test_style_template_download():
    user = await _make_user()
    async with _client() as c:
        r = await c.get("/api/styles/template")
        assert r.status_code == 200
        assert "style_name,keywords,description" in r.text
        assert "attachment" in r.headers["content-disposition"]
        # 模板自身可被导入接口解析（字段含逗号已正确加引号）
        r2 = await c.post("/api/styles/import",
                          files={"file": ("t.csv", r.content, "text/csv")},
                          data={"actor": user})
        assert r2.json()["imported"] == 2 and not r2.json()["errors"]


# ---------- 使用中学习：统计 / 默认风格 / 反查 ----------

async def _make_task(user_name: str, style: str, status: str) -> uuid.UUID:
    async with SessionLocal() as s:
        uid = (await s.execute(text("SELECT id FROM users WHERE name = :n"),
                               {"n": user_name})).scalar()
        t = Task(idempotency_key=f"k-{_uniq()}", query="q", content_type="x",
                 status=status, created_by=uid, gen_image_style=style)
        s.add(t)
        await s.commit()
        return t.id


@pytest.mark.asyncio
async def test_style_stats_aggregation():
    user = await _make_user()
    # 治愈暖彩：3 任务（2 通过 1 驳回）；真实摄影：1 任务（排队中，不算已审）
    for status in ("approved", "approved", "rejected"):
        await _make_task(user, "治愈暖彩", status)
    tid = await _make_task(user, "真实摄影", "draft")
    await _make_task(user, None, "approved")   # 无风格不计入
    # 定点重生成次数：被替换的 AI 配图历史版本（is_active=false）
    async with SessionLocal() as s:
        for i in range(2):
            s.add(Asset(task_id=tid, page_index=i + 1, source_type="ai_generated",
                        copyright_status="clear", hash=f"h{i}", is_active=False))
        await s.commit()
    async with _client() as c:
        r = await c.get(f"/api/styles/stats?actor={user}")
        body = r.json()
        items = {i["style_name"]: i for i in body["items"]}
        assert set(items) == {"治愈暖彩", "真实摄影"}
        heal = items["治愈暖彩"]
        assert heal["total"] == 3 and heal["approved"] == 2 and heal["rejected"] == 1
        assert abs(heal["approval_rate"] - 2 / 3) < 1e-3
        assert heal["regen_count"] == 0
        photo = items["真实摄影"]
        assert photo["total"] == 1 and photo["approval_rate"] is None
        assert photo["regen_count"] == 2
        assert body["default_style"] is None


@pytest.mark.asyncio
async def test_default_style_set_and_clear():
    user = await _make_user()
    async with _client() as c:
        r = await c.post("/api/styles/default",
                         json={"actor": user, "style_name": "治愈暖彩"})
        assert r.status_code == 200 and r.json()["default_style"] == "治愈暖彩"
        r = await c.get(f"/api/styles/stats?actor={user}")
        assert r.json()["default_style"] == "治愈暖彩"
        r = await c.delete(f"/api/styles/default?actor={user}")
        assert r.status_code == 200
        r = await c.get(f"/api/styles/stats?actor={user}")
        assert r.json()["default_style"] is None
        # 空风格名 → 422
        r = await c.post("/api/styles/default", json={"actor": user, "style_name": " "})
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_style_lookup():
    user = await _make_user()
    async with _client() as c:
        # 内置风格
        r = await c.get(f"/api/styles/lookup?style_name=治愈暖彩&actor={user}")
        assert r.json()["description"].startswith("柔和暖色调")
        # 未知风格 → 空描述
        r = await c.get(f"/api/styles/lookup?style_name=不存在&actor={user}")
        assert r.json()["description"] == ""
