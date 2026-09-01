import uuid
import pytest
from unittest.mock import patch, AsyncMock
from sqlalchemy import select, func
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.models.assets import Asset
from src.pipeline.nodes import node_entity_bind


def _png_bytes(w, h, color=(200, 30, 30)):
    from io import BytesIO
    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


_SMALL = _png_bytes(100, 100)        # 低质：低于 640x480 阈值
_BIG = _png_bytes(1200, 1600)        # 达标
_MID = _png_bytes(800, 600)          # 达标但像素更少


def _candidates(n_small, sizes):
    """sizes: list of bytes per candidate（大图/小图），返回 search_image 风格列表。"""
    return [{"title": f"t{i}", "image_url": f"http://img.test/{i}.png",
             "source": "bing", "engine": "bing"} for i in range(n_small)]


async def _fetch_by_url(image_url, timeout=60):
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
async def test_entity_bind_searches_for_general():
    """通用模式也搜实景图（2026-09-01 通用启用实景图）：整词搜索，与 single 同策略。"""
    tid = await _make_task(mode="general", query="通用内容")
    cands = _candidates(6, None)

    async def fetch(url, timeout=60):
        n = int(url.rsplit("/", 1)[1].split(".")[0])
        return _png_bytes(1200, 1600, (n * 20 % 255, 30, 30)), "image/png"

    with patch("src.gateway.image_search.search_image",
               new=AsyncMock(return_value=cands)) as mock_search, \
         patch("src.pipeline.nodes.fetch_image_bytes", new=fetch):
        out = await node_entity_bind({"task_id": tid})
    mock_search.assert_called()
    args, kwargs = mock_search.call_args
    assert args[0].endswith(" 高清") and kwargs["count"] == 25
    assert out["searched_images"] == 6   # 6 张全达标（内容各异，不被 md5 排重）
    async with SessionLocal() as session:
        cnt = (await session.execute(
            select(func.count()).select_from(Asset).where(Asset.task_id == tid))).scalar_one()
        assert cnt == 6


@pytest.mark.asyncio
async def test_entity_bind_keeps_all_passing_no_cap():
    """无最高限制（2026-08-27）：10 张候选全部达标 → 10 张全保留（旧逻辑截前 6）。"""
    tid = await _make_task(mode="single")
    cands = _candidates(10, None)

    async def fetch(url, timeout=60):
        n = int(url.rsplit("/", 1)[1].split(".")[0])
        return _png_bytes(1200, 1600, (n * 20 % 255, 30, 30)), "image/png"

    with patch("src.gateway.image_search.search_image",
               new=AsyncMock(return_value=cands)) as mock_search, \
         patch("src.pipeline.nodes.fetch_image_bytes", new=fetch):
        out = await node_entity_bind({"task_id": tid})
    args, kwargs = mock_search.call_args
    assert args[0].endswith(" 高清") and kwargs["count"] == 25
    assert out["searched_images"] == 10
    assert out["ref_filtered"] == 0
    assert out["ref_dupes"] == 0
    async with SessionLocal() as session:
        assets = (await session.execute(
            select(Asset).where(Asset.task_id == tid)
            .order_by(Asset.page_index))).scalars().all()
        assert len(assets) == 10
        for a in assets:
            assert a.source_type == "official"
            assert a.image_url.startswith("/static/generated/")  # 已本地化
            assert a.origin_url and a.origin_url.startswith("http://img.test/")


@pytest.mark.asyncio
async def test_entity_bind_filters_low_quality_and_dedupes():
    """最低限制 + 排重 + 保底：3 张达标 distinct 保留；1 张内容与第 1 张重复被
    排重；其余小图过滤，但因达标不足 6 张，用 3 张小图补齐保底（最少 6 张）。"""
    tid = await _make_task(mode="single")
    cands = _candidates(10, None)
    big0 = _png_bytes(1200, 1600, (10, 30, 30))

    async def fetch(url, timeout=60):
        n = int(url.rsplit("/", 1)[1].split(".")[0])
        data = {0: big0, 2: _png_bytes(1200, 1600, (40, 30, 30)),
                4: _MID, 6: big0}.get(n, _SMALL)  # 6 与 0 内容相同 → 排重
        return data, "image/png"

    with patch("src.gateway.image_search.search_image",
               new=AsyncMock(return_value=cands)), \
         patch("src.pipeline.nodes.fetch_image_bytes", new=fetch):
        out = await node_entity_bind({"task_id": tid})
    assert out["searched_images"] == 6   # 3 达标 + 3 张次优补齐保底
    assert out["ref_filtered"] == 6
    assert out["ref_dupes"] == 1
    async with SessionLocal() as session:
        assets = (await session.execute(
            select(Asset).where(Asset.task_id == tid))).scalars().all()
        assert len(assets) == 6
        localized = [a for a in assets if a.image_url.startswith("/static/generated/")]
        fallback = [a for a in assets if a.image_url.startswith("http://img.test/")]
        assert len(localized) == 3 and len(fallback) == 3
        assert len({a.hash for a in localized}) == 3  # 达标图内容 md5 两两不同


@pytest.mark.asyncio
async def test_entity_bind_fallback_when_all_low_quality():
    """全部不达标：用次优图补齐保底 6 张（最少 6 张，低质图仅在保底时使用）。"""
    tid = await _make_task(mode="single")
    cands = _candidates(10, None)

    async def fetch(url, timeout=60):
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
            assert a.image_url.startswith("http://img.test/")  # 保底图保留原地址
            assert a.origin_url is None


@pytest.mark.asyncio
async def test_entity_bind_compare_hd_queries():
    """compare 模式：每主体两组搜索词均带「高清」；mock 模式下不下载不过滤，
    但按 URL 排重——4 组搜回同一批 URL，只有第 1 组的 6 张被保留。"""
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
    # mock：URL 排重后仅首组 6 张入库，其余 18 张判重跳过
    assert out["searched_images"] == 6
    assert out["ref_filtered"] == 0
    assert out["ref_dupes"] == 18


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

    async def fetch(url, timeout=60):
        n = int(url.rsplit("/", 1)[1].split(".")[0])
        if n < 5:
            return _png_bytes(1200, 1600, (n * 40 + 10, 30, 30)), "image/png"
        return _SMALL, "image/png"

    with patch("src.gateway.image_search.search_image",
               new=AsyncMock(return_value=cands)) as mock_search, \
         patch("src.pipeline.nodes.fetch_image_bytes", new=fetch):
        async with _client() as ac:
            r = await ac.post(f"/api/tasks/{tid}/ref_search",
                              json={"actor": owner, "query": "炸锅 外观 高清"})
            assert r.status_code == 200
            body = r.json()
    assert body["added"] == 6 and body["filtered"] == 3  # 5 张达标 + 1 张次优补齐保底 6
    assert body["dupes"] == 0
    assert mock_search.call_args.args[0] == "炸锅 外观 高清"
    async with SessionLocal() as s:
        assets = (await s.execute(
            select(Asset).where(Asset.task_id == tid))).scalars().all()
        assert len(assets) == 6
        assert all(a.subject == "炸锅 外观 高清" for a in assets)


@pytest.mark.asyncio
async def test_ref_search_non_owner_404():
    owner = await _make_user(role="admin")
    other = await _make_user(role="C")
    tid = await _owned_task(owner)
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{tid}/ref_search", json={"actor": other})
        assert r.status_code == 404


# ---------- 手动上传参考图 ----------

@pytest.mark.asyncio
async def test_ref_upload_adds_dedupes_and_rejects():
    """上传 3 个文件：2 张 distinct 图入库；1 张与其中一张内容重复被排重；
    再传 1 个非图文件被拒收。"""
    owner = await _make_user(role="admin")
    tid = await _owned_task(owner)
    img1 = _png_bytes(1000, 800, (11, 22, 33))
    img2 = _png_bytes(1000, 800, (44, 55, 66))
    files = [
        ("files", ("a.png", img1, "image/png")),
        ("files", ("b.png", img2, "image/png")),
        ("files", ("c.png", img1, "image/png")),   # 与 a 内容重复
        ("files", ("d.txt", b"not an image", "text/plain")),
    ]
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{tid}/ref_upload",
                          data={"actor": owner, "subject": "A:某校"}, files=files)
        assert r.status_code == 200
        body = r.json()
    assert body["added"] == 2 and body["dupes"] == 1 and body["rejected"] == 1
    assert body["subject"] == "A:某校"
    async with SessionLocal() as s:
        assets = (await s.execute(
            select(Asset).where(Asset.task_id == tid))).scalars().all()
        assert len(assets) == 2
        for a in assets:
            assert a.source_type == "official"
            assert a.subject == "A:某校"
            assert a.model_version == "upload"
            assert a.image_url.startswith("/static/generated/")
    # 再次上传同一批 → 与库存 hash 撞重，全部排重
    async with _client() as ac:
        r = await ac.post(f"/api/tasks/{tid}/ref_upload",
                          data={"actor": owner},
                          files=[("files", ("a2.png", img1, "image/png"))])
        assert r.json()["dupes"] == 1 and r.json()["added"] == 0


@pytest.mark.asyncio
async def test_ref_upload_non_owner_404():
    owner = await _make_user(role="admin")
    other = await _make_user(role="C")
    tid = await _owned_task(owner)
    async with _client() as ac:
        r = await ac.post(
            f"/api/tasks/{tid}/ref_upload", data={"actor": other},
            files=[("files", ("a.png", _png_bytes(1000, 800), "image/png"))])
        assert r.status_code == 404


# ---------- 候选池放大 + 补搜循环 + 并发下载（2026-08-28） ----------

def _uniq_cands(tag, n):
    """n 个不同 URL 的候选（供补搜测试按搜索词配置返回）。"""
    return [{"title": f"{tag}{i}", "image_url": f"http://img.test/{tag}{i}.png",
             "source": "bing", "engine": "bing"} for i in range(n)]


async def _fetch_big_distinct(url, timeout=60):
    """每张 URL 返回内容互不相同的大图（按 URL 取色，避免 md5 排重）。"""
    import hashlib
    h = hashlib.md5(url.encode()).hexdigest()
    return _png_bytes(1200, 1600, (int(h[:2], 16), int(h[2:4], 16), int(h[4:6], 16))), "image/png"


@pytest.mark.asyncio
async def test_entity_bind_rescues_when_unit_short():
    """补搜触发：首轮只拿到 3 张达标（<6）→ 用「实拍」补搜一轮补足，不触发第二轮。"""
    tid = await _make_task(mode="single", query="空气炸锅 测评")
    plan = {"空气炸锅 测评 高清": _uniq_cands("a", 3),
            "空气炸锅 测评 实拍": _uniq_cands("b", 5)}
    calls = []

    async def fake_search(q, count=0):
        calls.append((q, count))
        return plan.get(q, [])

    with patch("src.gateway.image_search.search_image", side_effect=fake_search), \
         patch("src.pipeline.nodes.fetch_image_bytes", new=_fetch_big_distinct):
        out = await node_entity_bind({"task_id": tid})
    assert calls == [("空气炸锅 测评 高清", 25), ("空气炸锅 测评 实拍", 12)]
    assert out["searched_images"] == 8
    assert out["ref_rescoped"] == 1
    assert out["ref_filtered"] == 0 and out["ref_dupes"] == 0
    async with SessionLocal() as session:
        assets = (await session.execute(
            select(Asset).where(Asset.task_id == tid))).scalars().all()
        assert len(assets) == 8
        assert len([a for a in assets if "补搜" in a.subject]) == 5


@pytest.mark.asyncio
async def test_entity_bind_rescue_two_rounds_then_stops():
    """补搜最多 2 轮：两轮后仍不足 6 张也不再补（变体词用完即止）。"""
    tid = await _make_task(mode="single", query="空气炸锅 测评")
    plan = {"空气炸锅 测评 高清": _uniq_cands("a", 2),
            "空气炸锅 测评 实拍": _uniq_cands("b", 1),
            "空气炸锅 测评 外观 场景": _uniq_cands("c", 1)}
    calls = []

    async def fake_search(q, count=0):
        calls.append(q)
        return plan.get(q, [])

    with patch("src.gateway.image_search.search_image", side_effect=fake_search), \
         patch("src.pipeline.nodes.fetch_image_bytes", new=_fetch_big_distinct):
        out = await node_entity_bind({"task_id": tid})
    assert calls == ["空气炸锅 测评 高清", "空气炸锅 测评 实拍", "空气炸锅 测评 外观 场景"]
    assert out["searched_images"] == 4
    assert out["ref_rescoped"] == 2


@pytest.mark.asyncio
async def test_entity_bind_no_rescue_when_enough():
    """补搜不触发：首轮即达 6 张以上，不再发起变体词搜索。"""
    tid = await _make_task(mode="single", query="空气炸锅 测评")
    calls = []

    async def fake_search(q, count=0):
        calls.append(q)
        return _uniq_cands("a", 8)

    with patch("src.gateway.image_search.search_image", side_effect=fake_search), \
         patch("src.pipeline.nodes.fetch_image_bytes", new=_fetch_big_distinct):
        out = await node_entity_bind({"task_id": tid})
    assert calls == ["空气炸锅 测评 高清"]
    assert out["searched_images"] == 8
    assert out["ref_rescoped"] == 0


@pytest.mark.asyncio
async def test_entity_bind_rescue_dedupes_across_rounds():
    """补搜仍走同一 seen 排重：补搜返回的 URL/内容与首轮重复时不重复收进。"""
    tid = await _make_task(mode="single", query="空气炸锅 测评")
    same = _uniq_cands("a", 3)          # 首轮 3 张
    plan = {"空气炸锅 测评 高清": same,
            "空气炸锅 测评 实拍": same + _uniq_cands("b", 4)}  # 3 张重复 + 4 张新图
    calls = []

    async def fake_search(q, count=0):
        calls.append(q)
        return plan.get(q, [])

    with patch("src.gateway.image_search.search_image", side_effect=fake_search), \
         patch("src.pipeline.nodes.fetch_image_bytes", new=_fetch_big_distinct):
        out = await node_entity_bind({"task_id": tid})
    assert out["searched_images"] == 7       # 3 + 4，重复 3 张被排重
    assert out["ref_rescoped"] == 1
    assert out["ref_dupes"] == 3


@pytest.mark.asyncio
async def test_download_quality_refs_concurrent_dedupe():
    """并发下载的排重正确性：URL 重复 + 不同 URL 同内容 md5 都被判重；
    min_keep 保底仍生效（达标不足时用次优补齐）。"""
    from src.pipeline.nodes import _download_quality_refs
    big = _png_bytes(1200, 1600, (10, 30, 30))
    cands = [
        {"title": "1", "image_url": "http://img.test/d1.png", "engine": "bing"},
        {"title": "1d", "image_url": "http://img.test/d1.png", "engine": "bing"},  # URL 重复
        {"title": "2", "image_url": "http://img.test/d2.png", "engine": "bing"},   # 同内容 md5
        {"title": "3", "image_url": "http://img.test/d3.png", "engine": "bing"},   # 小图
    ]

    async def fetch(url, timeout=60):
        return (big if not url.endswith("d3.png") else _SMALL), "image/png"

    with patch("src.pipeline.nodes.fetch_image_bytes", new=fetch), \
         patch("src.pipeline.nodes._persist_image",
               side_effect=lambda tid_, i, tag, data, ct: f"/static/generated/r{i}.png"):
        refs, filtered, dupes = await _download_quality_refs(
            "t", cands, 1, "x", min_keep=1)
    assert dupes == 2          # 1 次 URL 级 + 1 次内容 md5 级
    assert len(refs) == 1      # 仅 1 张 distinct 达标图
    assert refs[0]["url"].startswith("/static/generated/")
    assert filtered == 1       # 小图被质量过滤


# ---------- 素材库复用（2026-09-01 通用启用实景图配套） ----------


def _lib_asset(task_id, i, hash_, query="空气炸锅 测评", subject=None,
               image_url=None):
    return Asset(task_id=task_id, page_index=i,
                 subject=subject if subject is not None else query,
                 source_type="official", copyright_status="unknown",
                 hash=hash_, image_url=image_url or f"/static/generated/{hash_}.png",
                 origin_url=f"http://origin.test/{hash_}.png",
                 model_version="bing", search_query=f"{query} 高清",
                 is_illustration=False)


@pytest.mark.asyncio
async def test_library_match_keyword_and_exclusions():
    """素材库匹配：关键词双向包含命中；本任务/不相关/死链/同 hash 重复的排除。"""
    from src.pipeline.nodes import _match_library_assets
    old = await _make_task(mode="single", query="空气炸锅 测评")
    new = await _make_task(mode="general", query="空气炸锅 测评")
    async with SessionLocal() as session:
        session.add(_lib_asset(old, 1, "h1"))
        session.add(_lib_asset(old, 2, "h2"))
        session.add(_lib_asset(old, 3, "h1"))            # 同 hash 重复 → 只取一张
        session.add(_lib_asset(old, 4, "h3", query="破壁机"))  # 不相关
        session.add(_lib_asset(new, 5, "h4"))            # 本任务素材 → 排除
        await session.commit()
    async with SessionLocal() as session:
        with patch("src.pipeline.nodes._library_file_ok",
                   side_effect=lambda url: "h2" not in url):  # h2 文件已删 → 死链
            hits = await _match_library_assets(session, new, "空气炸锅 测评")
    assert {h.hash for h in hits} == {"h1"}


@pytest.mark.asyncio
async def test_entity_bind_reuses_library_and_skips_search():
    """复用 ≥ 保底 6 张：直接挂载到本任务并跳过搜索（省 API），model_version=library。"""
    old = await _make_task(mode="single", query="空气炸锅 测评")
    async with SessionLocal() as session:
        for i in range(7):
            session.add(_lib_asset(old, i + 1, f"h{i}"))
        await session.commit()
    new = await _make_task(mode="general", query="空气炸锅 测评")
    with patch("src.pipeline.nodes._library_file_ok", return_value=True), \
         patch("src.gateway.image_search.search_image",
               new=AsyncMock(return_value=[])) as mock_search:
        out = await node_entity_bind({"task_id": new})
    assert out["ref_reused"] == 7
    assert out["searched_images"] == 7
    mock_search.assert_not_called()
    async with SessionLocal() as session:
        assets = (await session.execute(
            select(Asset).where(Asset.task_id == new))).scalars().all()
    assert len(assets) == 7
    assert all(a.model_version == "library" for a in assets)
    # 复用行保留原图的本地路径与溯源地址（共享磁盘文件，不重复下载）
    assert all(a.image_url.startswith("/static/generated/") for a in assets)
    assert all(a.origin_url and a.origin_url.startswith("http://origin.test/")
               for a in assets)


@pytest.mark.asyncio
async def test_entity_bind_library_short_then_searches():
    """复用不足 6 张：复用的挂载 + 照常搜索补齐，同 hash 不再重复收进。"""
    old = await _make_task(mode="single", query="空气炸锅 测评")
    async with SessionLocal() as session:
        for i in range(2):
            session.add(_lib_asset(old, i + 1, f"h{i}"))
        await session.commit()
    new = await _make_task(mode="general", query="空气炸锅 测评")
    cands = _candidates(8, None)  # 偶大奇小 → 4 张达标

    async def fetch(url, timeout=60):
        n = int(url.rsplit("/", 1)[1].split(".")[0])
        if n % 2 == 0:
            return _png_bytes(1200, 1600, (n * 20 % 255, 30, 30)), "image/png"
        return _SMALL, "image/png"

    with patch("src.pipeline.nodes._library_file_ok", return_value=True), \
         patch("src.gateway.image_search.search_image",
               new=AsyncMock(return_value=cands)) as mock_search, \
         patch("src.pipeline.nodes.fetch_image_bytes", new=fetch):
        out = await node_entity_bind({"task_id": new})
    assert out["ref_reused"] == 2
    mock_search.assert_called()
    assert out["searched_images"] == 2 + 6  # 2 复用 + 4 达标 + 2 次优保底
    async with SessionLocal() as session:
        assets = (await session.execute(
            select(Asset).where(Asset.task_id == new)
            .order_by(Asset.page_index))).scalars().all()
    assert [a.model_version for a in assets[:2]] == ["library", "library"]
