import uuid
import pytest
from unittest.mock import patch, AsyncMock
from sqlalchemy import select, func
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.models.assets import Asset
from src.pipeline.nodes import node_entity_bind


def _png_bytes(w, h):
    from io import BytesIO
    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


_SMALL = _png_bytes(100, 100)        # 低质：低于 640x480 阈值
_BIG = _png_bytes(1200, 1600)        # 达标
_MID = _png_bytes(800, 600)          # 达标但像素更少


def _candidates(n_small, sizes):
    """sizes: list of bytes per candidate（大图/小图），返回 search_image 风格列表。"""
    return [{"title": f"t{i}", "image_url": f"http://img.test/{i}.png",
             "source": "bing", "engine": "bing"} for i in range(n_small)]


async def _fetch_by_url(image_url):
    """按 URL 尾号奇偶返回大/小图字节（偶大奇小）。"""
    n = int(image_url.rsplit("/", 1)[1].split(".")[0])
    return (_BIG if n % 2 == 0 else _SMALL), "image/png"


async def _make_task(mode="single", query="空气炸锅 测评"):
    async with SessionLocal() as session:
        task = Task(idempotency_key=f"e-{uuid.uuid4().hex[:8]}", query=query,
                    content_type="generic", mode=mode)
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task.id


@pytest.mark.asyncio
async def test_entity_bind_skips_search_for_general():
    tid = await _make_task(mode="general", query="通用内容")
    with patch("src.gateway.image_search.search_image") as mock_search:
        out = await node_entity_bind({"task_id": tid})
    assert out["searched_images"] == 0
    mock_search.assert_not_called()
    async with SessionLocal() as session:
        cnt = (await session.execute(
            select(func.count()).select_from(Asset).where(Asset.task_id == tid))).scalar_one()
        assert cnt == 0


@pytest.mark.asyncio
async def test_entity_bind_filters_low_quality_and_keeps_topk():
    """10 张候选：3 张达标（2 大 1 中），7 张小图 → 只保留 3 张达标图并本地化。"""
    tid = await _make_task(mode="single")
    cands = _candidates(10, None)

    async def fetch(url):
        n = int(url.rsplit("/", 1)[1].split(".")[0])
        data = {0: _BIG, 2: _BIG, 4: _MID}.get(n, _SMALL)
        return data, "image/png"

    with patch("src.gateway.image_search.search_image",
               new=AsyncMock(return_value=cands)) as mock_search, \
         patch("src.pipeline.nodes.fetch_image_bytes", new=fetch):
        out = await node_entity_bind({"task_id": tid})
    # 高清搜索词
    args, kwargs = mock_search.call_args
    assert args[0].endswith(" 高清") and kwargs["count"] == 10
    assert out["searched_images"] == 3
    assert out["ref_filtered"] == 7
    async with SessionLocal() as session:
        assets = (await session.execute(
            select(Asset).where(Asset.task_id == tid)
            .order_by(Asset.page_index))).scalars().all()
        assert len(assets) == 3
        for a in assets:
            assert a.source_type == "official"
            assert a.image_url.startswith("/static/generated/")  # 已本地化
            assert a.origin_url and a.origin_url.startswith("http://img.test/")


@pytest.mark.asyncio
async def test_entity_bind_fallback_when_all_low_quality():
    """全部不达标：回退原地址前 6 张，参考图不为空。"""
    tid = await _make_task(mode="single")
    cands = _candidates(10, None)

    async def fetch(url):
        return _SMALL, "image/png"

    with patch("src.gateway.image_search.search_image",
               new=AsyncMock(return_value=cands)), \
         patch("src.pipeline.nodes.fetch_image_bytes", new=fetch):
        out = await node_entity_bind({"task_id": tid})
    assert out["searched_images"] == 6
    assert out["ref_filtered"] == 10
    async with SessionLocal() as session:
        assets = (await session.execute(
            select(Asset).where(Asset.task_id == tid))).scalars().all()
        assert len(assets) == 6
        for a in assets:
            assert a.image_url.startswith("http://img.test/")  # 回退保留原地址
            assert a.origin_url is None


@pytest.mark.asyncio
async def test_entity_bind_compare_hd_queries():
    """compare 模式：每主体两组搜索词均带「高清」，mock 模式下不过滤直接保留。"""
    tid = await _make_task(mode="compare", query="A校 vs B校 怎么选")
    cands = _candidates(6, None)
    with patch("src.pipeline.nodes._split_compare_subjects",
               new=AsyncMock(return_value=("A校", "B校"))), \
         patch("src.gateway.image_search.search_image",
               new=AsyncMock(return_value=cands)) as mock_search, \
         patch("src.config.settings.mock_image_gen", True):
        out = await node_entity_bind({"task_id": tid})
    queries = [c.args[0] for c in mock_search.call_args_list]
    assert queries == ["A校 高清", "A校 细节 侧面 高清", "B校 高清", "B校 细节 侧面 高清"]
    assert out["subjects"] == ["A校", "B校"]
    # mock：每组 6 张候选按 keep 截断 3/2/3/2 = 10 张
    assert out["searched_images"] == 10
    assert out["ref_filtered"] == 0


# ---------- 参考图人工维护端点 ----------

from httpx import AsyncClient, ASGITransport
from src.api.main import app


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


async def _owned_task(owner_name, mode="single"):
    """建任务并归属 owner（created_by=其用户 id）。"""
    from sqlalchemy import text as _text
    tid = await _make_task(mode=mode)
    async with SessionLocal() as s:
        uid = (await s.execute(_text(
            "SELECT id FROM users WHERE name = :n"), {"n": owner_name})).scalar_one()
        await s.execute(_text(
            "UPDATE tasks SET created_by = :u WHERE id = :t"), {"u": uid, "t": tid})
        await s.commit()
    return tid


async def _add_asset(tid, source_type="official"):
    import hashlib
    aid = uuid.uuid4()
    async with SessionLocal() as s:
        s.add(Asset(id=aid, task_id=tid, page_index=1, subject="t",
                    source_type=source_type, copyright_status="unknown",
                    hash=hashlib.md5(b"x").hexdigest(),
                    image_url="http://img.test/1.png", is_illustration=False))
        await s.commit()
    return aid


@pytest.mark.asyncio
async def test_delete_ref_asset_official_ok_ai_rejected():
    owner = await _make_user(role="admin")
    tid = await _owned_task(owner)
    aid_ref = await _add_asset(tid, "official")
    aid_ai = await _add_asset(tid, "ai_generated")
    async with _client() as ac:
        r = await ac.delete(f"/api/assets/{aid_ref}/ref", params={"actor": owner})
        assert r.status_code == 200 and r.json()["deleted"] is True
        r = await ac.delete(f"/api/assets/{aid_ai}/ref", params={"actor": owner})
        assert r.status_code == 400
    async with SessionLocal() as s:
        cnt = (await s.execute(
            select(func.count()).select_from(Asset).where(Asset.task_id == tid))).scalar_one()
        assert cnt == 1  # 只剩 AI 生成图


@pytest.mark.asyncio
async def test_delete_ref_asset_non_owner_404():
    owner = await _make_user(role="admin")
    other = await _make_user(role="C")
    tid = await _owned_task(owner)
    aid = await _add_asset(tid, "official")
    async with _client() as ac:
        r = await ac.delete(f"/api/assets/{aid}/ref", params={"actor": other})
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_ref_search_appends_quality_assets():
    owner = await _make_user(role="admin")
    tid = await _owned_task(owner)
    cands = _candidates(8, None)

    async def fetch(url):
        n = int(url.rsplit("/", 1)[1].split(".")[0])
        return (_BIG if n < 5 else _SMALL), "image/png"

    with patch("src.gateway.image_search.search_image",
               new=AsyncMock(return_value=cands)) as mock_search, \
         patch("src.pipeline.nodes.fetch_image_bytes", new=fetch):
        async with _client() as ac:
            r = await ac.post(f"/api/tasks/{tid}/ref_search",
                              json={"actor": owner, "query": "炸锅 外观 高清"})
            assert r.status_code == 200
            body = r.json()
    assert body["added"] == 4 and body["filtered"] == 3  # 5 达标取前 4
    assert mock_search.call_args.args[0] == "炸锅 外观 高清"
    async with SessionLocal() as s:
        assets = (await s.execute(
            select(Asset).where(Asset.task_id == tid))).scalars().all()
        assert len(assets) == 4
        assert all(a.subject == "炸锅 外观 高清" for a in assets)


@pytest.mark.asyncio
async def test_ref_search_non_owner_404():
    owner = await _make_user(role="admin")
    other = await _make_user(role="C")
    tid = await _owned_task(owner)
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{tid}/ref_search", json={"actor": other})
        assert r.status_code == 404
