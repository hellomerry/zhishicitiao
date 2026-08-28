"""风格关键词库 API（/api/styles）：CRUD / CSV 导入 / 模板下载。"""
import pytest
from httpx import AsyncClient, ASGITransport

from src.api.main import app


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_style_crud_flow():
    async with _client() as c:
        # 新增
        r = await c.post("/api/styles", json={
            "style_name": "科技蓝调", "keywords": "手机,数码", "description": "深蓝科技光感"})
        assert r.status_code == 200 and r.json()["ok"]
        # 列表
        r = await c.get("/api/styles")
        items = r.json()["items"]
        assert len(items) == 1 and items[0]["style_name"] == "科技蓝调"
        assert items[0]["enabled"] is True
        sid = items[0]["id"]
        # 同名覆盖更新（含停用）
        r = await c.post("/api/styles", json={
            "style_name": "科技蓝调", "keywords": "芯片", "description": "新描述",
            "enabled": False})
        assert r.status_code == 200
        items = (await c.get("/api/styles")).json()["items"]
        assert len(items) == 1
        assert items[0]["keywords"] == "芯片" and items[0]["enabled"] is False
        # 删除
        r = await c.delete(f"/api/styles/{sid}")
        assert r.status_code == 200
        assert (await c.get("/api/styles")).json()["items"] == []
        # 再删 → 404；坏 id → 400
        assert (await c.delete(f"/api/styles/{sid}")).status_code == 404
        assert (await c.delete("/api/styles/not-a-uuid")).status_code == 400


@pytest.mark.asyncio
async def test_style_name_required():
    async with _client() as c:
        r = await c.post("/api/styles", json={"style_name": "  "})
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_style_csv_import_idempotent_upsert():
    csv_text = ("style_name,keywords,description\n"
                "科技蓝调,手机,深蓝科技光感\n"
                "暖木家居,\"家具,装修\",暖木色系\n"
                ",无效行,缺风格名\n")
    async with _client() as c:
        r = await c.post("/api/styles/import",
                         files={"file": ("s.csv", csv_text.encode("utf-8"), "text/csv")})
        body = r.json()
        assert body["imported"] == 2 and len(body["errors"]) == 1
        # 同名再导入 = 覆盖更新，不新增
        r = await c.post("/api/styles/import",
                         files={"file": ("s.csv", "style_name,keywords,description\n科技蓝调,芯片,新描述\n".encode("utf-8"), "text/csv")})
        assert r.json()["imported"] == 1
        items = (await c.get("/api/styles")).json()["items"]
        assert len(items) == 2
        tech = next(i for i in items if i["style_name"] == "科技蓝调")
        assert tech["keywords"] == "芯片" and tech["description"] == "新描述"


@pytest.mark.asyncio
async def test_style_template_download():
    async with _client() as c:
        r = await c.get("/api/styles/template")
        assert r.status_code == 200
        assert "style_name,keywords,description" in r.text
        assert "attachment" in r.headers["content-disposition"]
        # 模板自身可被导入接口解析（字段含逗号已正确加引号）
        r2 = await c.post("/api/styles/import",
                          files={"file": ("t.csv", r.content, "text/csv")})
        assert r2.json()["imported"] == 2 and not r2.json()["errors"]
