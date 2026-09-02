import hashlib
import re
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import select
from src.config import settings
from src.db.session import SessionLocal
from src.gateway.failover import call_with_failover, DEEPSEEK_MODEL, KIMI_MODEL
from src.gateway.ocr import fetch_image_bytes
from src.quality.rules import check_rules

# 生成图本地持久化目录（通过 /static 挂载直接可访问）
GENERATED_DIR = Path(__file__).resolve().parent.parent.parent / "static" / "generated"

_EXT_BY_CTYPE = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def _layout_offset_for(task_id) -> int:
    """分页布局/文字形式轮换起点（2026-09-02 反同质化）：按 task_id 取模，
    同一任务内恒定（6 页布局仍互不相同），不同任务页位映射不同——此前
    每个任务的第 N 页永远套第 N 种布局，套与套摆在一起一眼模板感。
    get_image_prompt(layout_offset) 与 text_composite.composite_page(offset)
    必须用本函数同一值（AI 预留留白区与合成落版对齐）。"""
    return int(hashlib.md5(str(task_id).encode()).hexdigest(), 16) % 6


def _persist_image(task_id, page_index, tag: str, data: bytes, ctype: str) -> str:
    """把图片字节落到 static/generated/，返回可浏览的本地路径。

    上游生图代理的 URL 会过期（实测隔天 404），必须在产出时立即本地化。
    """
    ext = _EXT_BY_CTYPE.get(ctype, ".png")
    name = f"{task_id}_{tag}{page_index}_{uuid.uuid4().hex[:8]}{ext}"
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    (GENERATED_DIR / name).write_bytes(data)
    return f"/static/generated/{name}"


def text_similarity(expected: str, actual: str) -> float:
    """分页文案 vs OCR 识别文字的字级相似度（0-1）。

    只保留汉字/字母/数字（忽略标点空白），用 difflib 比率衡量吻合度。
    低分通常意味着图上文字扭曲/伪汉字（gpt-image-2 中文渲染通病，2026-08-25 用户反馈）。
    """
    import difflib
    import re
    norm = lambda s: "".join(re.findall(r"[一-鿿A-Za-z0-9]", s or ""))
    e, a = norm(expected), norm(actual)
    if not e:
        return 1.0
    if not a:
        return 0.0
    return difflib.SequenceMatcher(None, e, a).ratio()

NODES = [
    "task_import", "entity_bind", "evidence_build", "draft_gen",
    "draft_polish", "rule_check", "page_split", "art_director", "asset_gen",
    "ocr_read", "cross_check", "risk_classify", "review_queue",
    "batch_signoff", "publish_snapshot"
]


async def execute_node(task_id, node_name: str, input_data: dict, node_fn=None):
    from src.pipeline.idempotency import check_or_record_node_event
    from src.stream.bus import bus
    tid = str(task_id)
    async with SessionLocal() as session:
        event = await check_or_record_node_event(
            session, task_id, node_name, input_data)
        if event is None:
            return {"skipped": True}
        start = datetime.now(timezone.utc)
        event.started_at = start
        await bus.publish("node_started", {"node": node_name}, task_id=tid)
        try:
            if node_fn:
                output = await node_fn(input_data)
            else:
                output = {"node": node_name, "input": input_data}
            event.finished_at = datetime.now(timezone.utc)
            event.cost_estimate_cny = output.get("cost_cny", 0)
            event.model_version = output.get("model_version")
            event.prompt_version = output.get("prompt_version")
            await session.commit()
            summary = _node_summary(node_name, output)
            summary["elapsed"] = round((event.finished_at - start).total_seconds(), 2)
            await bus.publish("node_finished", summary, task_id=tid)
            return output
        except Exception as e:
            event.finished_at = datetime.now(timezone.utc)
            event.error_class = type(e).__name__
            event.retry_count = (event.retry_count or 0) + 1
            await session.commit()
            await bus.publish("node_failed", {
                "node": node_name,
                "error": str(e),
                "traceback": traceback.format_exc(),
                "elapsed": round((event.finished_at - start).total_seconds(), 2),
            }, task_id=tid)
            # 限流类错误即时反馈给并发限制器（乘性减），不必等整条任务失败收尾
            from src.stream.scheduler import scheduler, is_throttled
            if is_throttled(e):
                await scheduler.limiter.report_throttle()
            raise


def _node_summary(node_name: str, output: dict) -> dict:
    """抽取节点输出的可读摘要 + 实际内容片段，供流式前端展示。"""
    s: dict = {"node": node_name}
    if node_name == "draft_gen":
        text = output.get("text", "")
        s["preview"] = text[:220]
        s["length"] = len(text)
        s["model"] = output.get("model_version")
    elif node_name == "draft_polish":
        s["polished"] = output.get("polished", False)
        if output.get("polished"):
            text = output.get("text", "")
            s["preview"] = text[:220]
            s["length"] = len(text)
            s["model"] = output.get("model_version")
    elif node_name == "asset_gen":
        s["count"] = output.get("asset_count", 0)
        s["image_urls"] = output.get("image_urls", [])
    elif node_name == "evidence_build":
        s["evidence_count"] = output.get("evidence_count", 0)
        s["conflicts"] = output.get("conflicts", [])
    elif node_name == "risk_classify":
        s["level"] = output.get("level")
        s["reasons"] = output.get("reasons", [])
    elif node_name == "entity_bind":
        s["searched_images"] = output.get("searched_images", 0)
    else:
        for k, v in output.items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                s[k] = v
    return s


async def _latest_draft_body(session, task_id):
    from src.models.drafts import Draft
    result = await session.execute(
        select(Draft).where(Draft.task_id == task_id).order_by(Draft.version.desc()))
    draft = result.scalars().first()
    return draft.body if draft else ""


_SUBJECT_SPLIT_PROMPT = """从下面的对比类查询中提取两个被对比的主体名称。
输出严格 JSON：{"a": "主体A名称", "b": "主体B名称"}，不要输出任何其他内容。
若查询并非两个主体的对比，输出 {"a": "", "b": ""}。

查询：{query}"""


def _heuristic_split_subjects(query: str):
    """兜底拆分：按常见对比连词切 query（不依赖 LLM）。返回 (a, b) 或 None。"""
    import re
    q = re.sub(r"[？?！!。.\s]+$", "", (query or "").strip())
    # 先剥掉结尾的决策措辞（怎么选/哪个好…），再用连词切分
    q = re.sub(r"(怎么选|哪个好|哪个值得买|选哪个|买哪个|哪个更适合)$", "", q).strip(" ，,：:")
    for sep in ("对比", " VS ", " vs ", "pk", "PK", "和", "与"):
        if sep in q:
            a, b = q.split(sep, 1)
            a = a.strip(" ，,：:")
            # b 可能还带「：小升初」类场景后缀，取主体名部分
            b = re.split(r"[：，,]", b)[0].strip(" ，,：:")
            if a and b:
                return a, b
    return None


async def _split_compare_subjects(query: str):
    """对比模式拆主体（2026-08-25 用户反馈③④：对比类必须双主体、多角度）。
    LLM 优先，失败退回连词启发式，再失败返回 None（退回整词搜索的旧行为）。"""
    import json
    import re
    try:
        r = await call_with_failover(
            _SUBJECT_SPLIT_PROMPT.replace("{query}", query),
            DEEPSEEK_MODEL, KIMI_MODEL, max_retries=1)
        m = re.search(r"\{[^{}]*\}", r.get("content") or "", re.S)
        if m:
            data = json.loads(m.group(0))
            a, b = (data.get("a") or "").strip(), (data.get("b") or "").strip()
            if a and b:
                return a, b
    except Exception:
        traceback.print_exc()  # 拆分失败不阻塞流水线，走启发式/整词兜底
    return _heuristic_split_subjects(query)


# ── 参考图质量过滤（2026-08-26 用户反馈：搜到的参考图质量差）────────
_REF_MIN_W, _REF_MIN_H = 640, 480  # 低于此分辨率的参考图判为低质


def _image_size(data: bytes):
    """读图片宽高，读不出（坏图/非图）返回 None。"""
    try:
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(data)) as img:
            return img.size
    except Exception:
        return None


async def _download_quality_refs(task_id, candidates, start_index, page_tag,
                                 seen: dict | None = None, min_keep: int = 6):
    """下载候选图并本地化：达最低分辨率的全保留（不设上限）+ 排重，
    不足 min_keep 张时用次优图补齐保底（2026-08-27 用户要求：实图最少 6 张）。

    保留策略：无最高限制，只有最低限制（宽高 ≥ 阈值）+ 排重——URL 级 + 内容
    md5 级两级排重；seen 由调用方跨搜索组维护，避免同一张实图被不同搜索词
    重复收进。达标图不足 min_keep 时，依次用「下载成功但不达标」（按像素降序）
    和「下载失败」（沿用原地址，展示层走代理兜底）补齐到 min_keep。

    并发下载（2026-08-28：串行 60s/张太慢、候选池放大后更跑不动）：先串行规划
    要下载的 URL（seen 互斥无竞态），再 Semaphore(8) 并发取字节（单张超时 20s），
    最后串行做尺寸判定/落盘/排重——返回结构与过滤口径与串行版完全一致。
    返回 (refs, filtered, dupes)，ref 含 url/origin/engine/hash（内容 md5）。
    """
    import asyncio
    if seen is None:
        seen = {"hashes": set(), "urls": set()}
    # 1) 串行规划：URL 级排重，确定本轮要下载的候选
    planned = []   # [(ref, url)]
    dupes = 0
    for it in candidates:
        url = it["image_url"]
        if url in seen["urls"]:
            dupes += 1
            continue
        seen["urls"].add(url)
        planned.append(({"url": url, "origin": None,
                         "engine": it.get("engine", "search"),
                         "hash": hashlib.md5(url.encode()).hexdigest()}, url))
    # 2) 并发取字节：Semaphore(8) 限流，结果顺序与 planned 一致
    sem = asyncio.Semaphore(8)

    async def _one(url):
        async with sem:
            return await fetch_image_bytes(url, timeout=20)

    results = await asyncio.gather(*(_one(u) for _, u in planned),
                                   return_exceptions=True)
    # 3) 串行判定：尺寸过滤 / 内容 md5 排重 / 落盘
    good = []    # [(像素量, ref)] 达标
    lowres = []  # [(像素量或0, ref)] 下载成功但不达标（含读不出尺寸）
    failed = []  # [ref] 下载失败/空
    filtered = 0
    for (ref, url), r in zip(planned, results):
        if isinstance(r, Exception):
            filtered += 1
            failed.append(ref)
            continue
        data, ctype = r
        if not data:
            filtered += 1
            failed.append(ref)
            continue
        size = _image_size(data)
        if size and size[0] >= _REF_MIN_W and size[1] >= _REF_MIN_H:
            content_md5 = hashlib.md5(data).hexdigest()
            if content_md5 in seen["hashes"]:
                dupes += 1
                continue
            seen["hashes"].add(content_md5)
            path = _persist_image(task_id, start_index + len(good),
                                  f"ref_{page_tag}", data, ctype)
            good.append((size[0] * size[1],
                         {**ref, "url": path, "origin": url,
                          "hash": content_md5}))
        else:
            filtered += 1
            lowres.append(((size[0] * size[1]) if size else 0, ref))
    good.sort(key=lambda x: -x[0])
    lowres.sort(key=lambda x: -x[0])
    # 保底填充：达标不足 min_keep 张时，先用不达标但可下载的，再用下载失败的
    fill = max(0, min_keep - len(good))
    refs = ([r for _, r in good]
            + ([r for _, r in lowres] + failed)[:fill])
    return refs, filtered, dupes


# 补搜变体词（2026-08-28 用户反馈「实图还是太少」）：按主体跟踪达标+保底填充后
# 的张数，不足 6 张时依次用这些变体词补搜，每个主体最多补 2 轮（仍走同一 seen 排重）
_RESCUE_QUERIES = ("{name} 实拍", "{name} 外观 场景")
_RESCUE_COUNT = 12        # 补搜每轮抓取数
_REF_MIN_KEEP = 6         # 每主体实图保底张数


# ── 素材库复用（2026-09-01 通用启用实景图配套）────────────────────
# 历史任务沉淀的 official 实图就是本地素材库：搜图前先按关键词匹配库中素材，
# 命中直接挂载到本任务（共享磁盘文件与内容 hash，免重复下载、省搜索 API）。
# 匹配口径：query 与素材的搜索词/主体标签规范化（剥搜索修饰词与空白）后
# 双向包含；文件必须还在磁盘（回收站彻底删除会清文件，死链不复用）。
# 库中图都是公网资源、不含任务隐私，全局共享复用。
_LIBRARY_STRIP = re.compile(r"(高清|细节|侧面|实拍|外观|场景|（补搜）)")
_LIBRARY_MAX_ROWS = 500    # 库扫描上限（最近收录优先）
_LIBRARY_LIMIT = 12        # 单任务最多复用张数


def _library_norm(s: str) -> str:
    return re.sub(r"\s+", "", _LIBRARY_STRIP.sub("", s or ""))


def _library_file_ok(image_url: str) -> bool:
    url = image_url or ""
    if not url.startswith("/static/"):
        return False
    local = Path(__file__).resolve().parent.parent.parent / url.lstrip("/")
    return local.exists()


async def _match_library_assets(session, task_id, query, limit=_LIBRARY_LIMIT):
    """按 query 关键词匹配素材库（排除本任务的 official 实图），最近收录优先，
    同内容 hash 只取一张。"""
    from src.models.assets import Asset
    key = _library_norm(query)
    if len(key) < 2:
        return []
    rows = (await session.execute(
        select(Asset).where(Asset.source_type == "official",
                            Asset.is_illustration == False,
                            Asset.task_id != task_id,
                            Asset.image_url.isnot(None))
        .order_by(Asset.created_at.desc())
        .limit(_LIBRARY_MAX_ROWS))).scalars().all()
    hits, seen_hashes = [], set()
    for a in rows:
        cand = _library_norm((a.search_query or "") + " " + (a.subject or ""))
        if len(cand) < 2 or not (key in cand or cand in key):
            continue
        if not _library_file_ok(a.image_url):
            continue
        if a.hash in seen_hashes:
            continue
        seen_hashes.add(a.hash)
        hits.append(a)
        if len(hits) >= limit:
            break
    return hits


async def node_entity_bind(input_data: dict) -> dict:
    """搜实景图/实物图，存为 official 素材（compare/single/general 有实图都作生图参考）。

    质量策略：搜索词带「高清」提高源头质量；下载后只按最低分辨率过滤 + 排重，
    达标的全部保留不设上限（2026-08-27 用户要求，mock 模式不下载不过滤不补搜，
    保留旧行为）。候选池 2026-08-28 放大（compare 每主体 20+12、single 25，
    bing limit=30 实测能返回 30 张）；每个主体搜完不足 6 张自动用变体词补搜
    （最多 2 轮）。ref_filtered 记录被质量过滤淘汰的张数，ref_dupes 记录排重
    跳过的张数，ref_rescoped 记录补搜轮次。
    """
    import hashlib
    from src.models.tasks import Task
    from src.models.assets import Asset
    from src.gateway.image_search import search_image
    async with SessionLocal() as session:
        task = (await session.execute(
            select(Task).where(Task.id == input_data["task_id"]))).scalar_one()
        query = task.query
        mode = task.mode or "general"
    # compare 模式拆主体 A/B 分搜（各带整体图 + 细节/侧面图，覆盖多角度对比），
    # 拆不出来时退回整词搜索（旧行为）；single/general 模式整词搜
    # （2026-09-01 通用启用实景图：相关性靠质量过滤+补搜+素材库兜底，
    # 一张没搜到时 asset_gen 自动回退纯 AI 生成）。
    # units：[(主体名, [(tag, 搜索词, 抓取数)])]——补搜按主体（unit）跟踪张数
    if mode == "compare":
        pair = await _split_compare_subjects(query)
    else:
        pair = None
    if pair:
        units = [(name, [(f"{label}:{name}", f"{name} 高清", 20),
                         (f"{label}:{name}（细节）", f"{name} 细节 侧面 高清", 12)])
                 for label, name in (("A", pair[0]), ("B", pair[1]))]
    else:
        units = [(query, [(query, f"{query} 高清", 25)])]
    filtered_total = 0
    dupes_total = 0
    rescoped_total = 0   # 补搜轮次（所有主体合计）
    search_calls = 0
    saved = 0
    # 跨搜索组共享排重集合：同一张实图不会被不同搜索词重复收进
    seen = {"hashes": set(), "urls": set()}
    # 素材库复用：历史任务沉淀的 official 实图按关键词匹配直接挂载（共享磁盘
    # 文件与 hash，免下载）；复用达保底张数则跳过搜索省 API，不足照常搜索补齐。
    # mock 模式跳过（与不下载/不过滤/不补搜一致，保留旧行为）
    reused = 0
    if not settings.mock_image_gen:
        async with SessionLocal() as session:
            hits = await _match_library_assets(session, input_data["task_id"], query)
            for a in hits:
                saved += 1
                reused += 1
                session.add(Asset(
                    task_id=input_data["task_id"], page_index=saved,
                    subject=a.subject or query, source_type="official",
                    copyright_status=a.copyright_status or "unknown",
                    hash=a.hash, image_url=a.image_url, origin_url=a.origin_url,
                    model_version="library",
                    search_query=a.search_query or query,
                    is_illustration=False))
                if a.hash:
                    seen["hashes"].add(a.hash)
                for u in (a.image_url, a.origin_url):
                    if u:
                        seen["urls"].add(u)
            await session.commit()
    if reused >= _REF_MIN_KEEP:
        return {"searched_images": saved, "ref_reused": reused,
                "ref_filtered": 0, "ref_dupes": 0, "ref_rescoped": 0,
                "subjects": list(pair) if pair else [], "cost_cny": 0.0}
    async with SessionLocal() as session:
        for name, groups in units:
            unit_kept = 0
            for tag, q, cnt in groups:
                got = await search_image(q, count=cnt)
                search_calls += 1
                if settings.mock_image_gen:
                    refs = []
                    filtered = dupes = 0
                    for g in got:
                        if g["image_url"] in seen["urls"]:
                            dupes += 1
                            continue
                        seen["urls"].add(g["image_url"])
                        refs.append({"url": g["image_url"], "origin": None,
                                     "engine": g.get("engine", "search"),
                                     "hash": hashlib.md5(
                                         g["image_url"].encode()).hexdigest()})
                else:
                    refs, filtered, dupes = await _download_quality_refs(
                        input_data["task_id"], got, saved + 1, tag, seen)
                filtered_total += filtered
                dupes_total += dupes
                for ref in refs:
                    saved += 1
                    session.add(Asset(
                        task_id=input_data["task_id"], page_index=saved,
                        subject=tag, source_type="official",
                        copyright_status="unknown",
                        hash=ref.get("hash") or hashlib.md5(
                            ref["url"].encode()).hexdigest(),
                        image_url=ref["url"], origin_url=ref["origin"],
                        model_version=ref["engine"],
                        search_query=q,
                        is_illustration=False))
                unit_kept += len(refs)
            # 补搜循环：本主体达标+保底后仍不足 6 张 → 变体词补搜（最多 2 轮，
            # 同一 seen 排重；补搜图打「（补搜）」标，compare 仍带 A:/B: 前缀，
            # 不影响 asset_gen 的分主体参考图路由）。mock 模式不补搜（测试依赖旧行为）
            rescoped = 0
            while (not settings.mock_image_gen and unit_kept < _REF_MIN_KEEP
                   and rescoped < len(_RESCUE_QUERIES)):
                q = _RESCUE_QUERIES[rescoped].format(name=name)
                rescoped += 1
                got = await search_image(q, count=_RESCUE_COUNT)
                search_calls += 1
                refs, filtered, dupes = await _download_quality_refs(
                    input_data["task_id"], got, saved + 1,
                    f"{groups[0][0]}（补搜）", seen)
                filtered_total += filtered
                dupes_total += dupes
                for ref in refs:
                    saved += 1
                    session.add(Asset(
                        task_id=input_data["task_id"], page_index=saved,
                        subject=f"{groups[0][0]}（补搜）", source_type="official",
                        copyright_status="unknown",
                        hash=ref.get("hash") or hashlib.md5(
                            ref["url"].encode()).hexdigest(),
                        image_url=ref["url"], origin_url=ref["origin"],
                        model_version=ref["engine"],
                        search_query=q,
                        is_illustration=False))
                unit_kept += len(refs)
            rescoped_total += rescoped
        await session.commit()
    return {"searched_images": saved, "ref_reused": reused,
            "ref_filtered": filtered_total,
            "ref_dupes": dupes_total, "ref_rescoped": rescoped_total,
            "subjects": list(pair) if pair else [],
            "cost_cny": settings.openserp_cost_per_call * search_calls}


# 信源可信度分级（2026-09-01 对齐人工审核 SOP 的采信优先级：
# gov/edu 官方信源 > 百科类 > 普通网页；原实现一律硬编码 P2）
def source_level_for(url: str) -> str:
    u = (url or "").lower()
    if any(k in u for k in ("gov.cn", "edu.cn", ".gov/", ".edu/")):
        return "P0"
    if any(k in u for k in ("baike.baidu.com", "wikipedia.org")):
        return "P2"
    return "P3"


async def node_evidence_build(input_data: dict) -> dict:
    from src.models.tasks import Task
    from src.models.entities import Claim, Evidence
    from src.models.review import Issue
    from src.gateway.web_search import web_search, deepseek_verify, detect_conflict
    async with SessionLocal() as session:
        task = (await session.execute(
            select(Task).where(Task.id == input_data["task_id"]))).scalar_one()
        query = task.query
    # 1. 豆包检索（结构化来源）
    results = await web_search(query, count=6)
    # 2. DeepSeek 联网验证（交叉校验）
    deepseek_text, verify_cost = await deepseek_verify(query)
    # 3. 争议检测：豆包来源 vs DeepSeek 结论的关键数字不一致
    conflicts = detect_conflict([r["summary"] or "" for r in results], deepseek_text)
    async with SessionLocal() as session:
        claim = Claim(task_id=input_data["task_id"], claim_text=query,
                      risk_level="P1", position=1)
        session.add(claim)
        await session.flush()
        for r in results:
            session.add(Evidence(claim_id=claim.id, source_url=r["url"] or "no-url",
                                 source_level=source_level_for(r["url"] or ""),
                                 excerpt=(r["summary"] or "")[:500],
                                 supports=True))
        # 4. 争议预警：创建 P1 问题单（事实审核 A 域）
        if conflicts:
            session.add(Issue(task_id=input_data["task_id"], role="A", priority="P1",
                              description="证据争议: " + "; ".join(conflicts)))
        await session.commit()
    return {"evidence_built": True, "evidence_count": len(results),
            "conflicts": conflicts,
            "cost_cny": settings.doubao_search_cost_per_call + verify_cost}


async def node_draft_gen(input_data: dict) -> dict:
    from src.models.tasks import Task
    from src.models.drafts import Draft
    from src.gateway.prompt_versions import get_effective_prompt
    async with SessionLocal() as session:
        task = (await session.execute(
            select(Task).where(Task.id == input_data["task_id"]))).scalar_one()
        query = task.query
        mode = task.mode or "general"
        owner_id = task.created_by
    template = await get_effective_prompt("draft_gen", mode, owner_id)
    # 标杆交付规范（迁移 015，借鉴 8003：真实成功案例提炼的标题/结构/口吻规律）
    from src.services.bench_rule import get_bench_rule
    bench = await get_bench_rule(mode)
    prompt = template + ("\n\n" + bench if bench else "") + "\n\n" + query
    prompt_version = f"draft_{mode}_v1"
    # 驳回重生成：审核意见与系统提示词、任务 query 一起作为处理依据，
    # 要求模型逐条修正，避免同类问题遗留到下一轮审核。
    regen = input_data.get("regen") or {}
    feedbacks = regen.get("feedback") or []
    if feedbacks:
        lines = "\n".join(f"{i}. {r}" for i, r in enumerate(feedbacks, 1))
        prompt += ("\n\n【重要：审核驳回反馈】本内容此前在人工审核中被驳回，"
                   "以下是审核员提出的全部修改意见：\n" + lines +
                   "\n请逐条针对性修正上述问题后重新创作，确保新内容不再出现同类问题。")
        prompt_version = f"draft_{mode}_v1_regen{regen.get('round', 1)}"
    result = await call_with_failover(prompt, DEEPSEEK_MODEL, KIMI_MODEL)
    async with SessionLocal() as session:
        from sqlalchemy import func
        max_v = (await session.execute(
            select(func.max(Draft.version)).where(
                Draft.task_id == input_data["task_id"]))).scalar() or 0
        session.add(Draft(
            task_id=input_data["task_id"], version=max_v + 1, body=result["text"],
            model_version=result["model_version"], prompt_version=prompt_version))
        await session.commit()
    return {"text": result["text"], "model_version": result["model_version"],
            "prompt_version": prompt_version, "cost_cny": result["cost_cny"],
            "degraded": result["degraded"]}


async def node_draft_polish(input_data: dict) -> dict:
    """校稿润色（对齐人工两轮校稿）：删夸大表述、削 AI 腔、控制字数、补免责声明。
    LLM 失败或输出异常时不阻塞流水线，沿用 draft_gen 原稿。"""
    from src.models.tasks import Task
    from src.models.drafts import Draft
    from src.gateway.prompt_versions import get_effective_prompt
    async with SessionLocal() as session:
        task = (await session.execute(
            select(Task).where(Task.id == input_data["task_id"]))).scalar_one()
        owner_id = task.created_by
        text = await _latest_draft_body(session, input_data["task_id"])
    if not text:
        raise RuntimeError("draft_polish: 缺少 draft_gen 产物")
    template = await get_effective_prompt("draft_polish", None, owner_id)
    prompt = template.replace("{body}", text)
    prompt_version = "draft_polish_v1"
    regen = input_data.get("regen") or {}
    if regen.get("feedback"):
        prompt_version = f"draft_polish_v1_regen{regen.get('round', 1)}"
    try:
        result = await call_with_failover(prompt, DEEPSEEK_MODEL, KIMI_MODEL)
    except Exception:
        return {"polished": False, "reason": "llm_unavailable"}
    out = (result["text"] or "").strip()
    if len(out) < 100:
        return {"polished": False, "reason": "output_too_short"}
    async with SessionLocal() as session:
        from sqlalchemy import func
        max_v = (await session.execute(
            select(func.max(Draft.version)).where(
                Draft.task_id == input_data["task_id"]))).scalar() or 0
        session.add(Draft(
            task_id=input_data["task_id"], version=max_v + 1, body=out,
            model_version=result["model_version"], prompt_version=prompt_version))
        await session.commit()
    return {"text": out, "polished": True, "model_version": result["model_version"],
            "prompt_version": prompt_version, "cost_cny": result["cost_cny"],
            "degraded": result["degraded"]}


async def node_rule_check(input_data: dict) -> dict:
    from src.models.drafts import RuleResult
    async with SessionLocal() as session:
        text = await _latest_draft_body(session, input_data["task_id"])
    title = text.split("\n")[0][:25] if text else ""
    results = check_rules(text, title)
    async with SessionLocal() as session:
        for r in results:
            session.add(RuleResult(
                task_id=input_data["task_id"], rule_name=r["rule_name"],
                passed=r["passed"], details=r["details"]))
        await session.commit()
    return {"rule_results": results, "all_passed": all(r["passed"] for r in results)}


def _split_pages(text: str, n: int = 6) -> list:
    """把整篇正文切成 n 页：优先按段落边界均衡分配，段落不足按句子，再退按字数硬切。
    不再截断正文（旧实现只取前 350 字、每页 58 字，句子被拦腰切断）。"""
    import re
    text = (text or "").strip()
    if not text:
        return [""] * n

    def balance(units: list) -> list:
        """按原顺序把 units 切成 n 段，长度尽量均衡（保阅读顺序）。"""
        total = sum(len(u) for u in units)
        target = max(1, -(-total // n))
        buckets, cur, cur_len = [], [], 0
        for u in units:
            if cur and cur_len + len(u) > target and len(buckets) < n - 1:
                buckets.append(cur)
                cur, cur_len = [], 0
            cur.append(u)
            cur_len += len(u)
        buckets.append(cur)
        return ["".join(b) for b in buckets]

    paras = [p for p in re.split(r"(\n+)", text) if p.strip()]
    if len([p for p in paras if not p.isspace()]) >= n and len(paras) >= n:
        pages = balance(paras)
    else:
        sents = re.split(r"(?<=[。！？；!?;])", text)
        sents = [s for s in sents if s.strip()]
        if len(sents) >= n:
            pages = balance(sents)
        else:
            chunk = max(1, -(-len(text) // n))
            pages = [text[i:i + chunk] for i in range(0, len(text), chunk)]
    while len(pages) < n:
        pages.append("")
    return pages[:n]


# 分页文案占位词黑名单（2026-08-31 真实 bug：LLM 把结构名当文案输出，
# 封面被渲染成「标题」两个大字）：整行恰为这些词的视为占位行剥除
_PLACEHOLDER_LINE_WORDS = {"标题", "副标题", "正文", "小标题", "大标题",
                           "封面", "封面标题", "内容", "结尾"}


def _strip_placeholder_lines(pages: list) -> list:
    """剥除分页文案里的占位词整行（如单独一行的「标题」），保留真实文案。"""
    cleaned = []
    for p in pages:
        lines = [ln for ln in str(p).splitlines()
                 if ln.strip().strip("：:") not in _PLACEHOLDER_LINE_WORDS]
        cleaned.append("\n".join(lines).strip())
    return cleaned


async def node_page_split(input_data: dict) -> dict:
    from src.models.drafts import PageCopy
    from src.models.tasks import Task
    from src.gateway.prompt_versions import get_effective_prompt
    async with SessionLocal() as session:
        text = await _latest_draft_body(session, input_data["task_id"])
        row = (await session.execute(
            select(Task.created_by, Task.mode)
            .where(Task.id == input_data["task_id"]))).first()
        owner_id = row[0] if row else None
        mode = (row[1] or "general") if row else "general"
    # 首选 LLM 按页写图上文案；解析失败/调用失败退回机械切割（保证节点不卡死）
    pages, model_version, cost = None, "mechanical", 0.0
    try:
        import json as _json
        template = await get_effective_prompt("page_split", None, owner_id)
        llm_prompt = (template.replace("{body}", text) if "{body}" in template
                      else template + "\n\n" + text)
        # 标杆交付规范（迁移 015）：图上文案规律注入分页写作
        from src.services.bench_rule import get_bench_rule
        bench = await get_bench_rule(mode)
        if bench:
            llm_prompt = bench + "\n\n" + llm_prompt
        result = await call_with_failover(llm_prompt, DEEPSEEK_MODEL, KIMI_MODEL)
        raw = result["text"].strip()
        if raw.startswith("```"):
            raw = raw.strip("`").lstrip("json").strip()
        arr = _json.loads(raw[raw.index("["):raw.rindex("]") + 1])
        arr = [str(p).strip() for p in arr if str(p).strip()]
        if len(arr) >= 6:
            pages = _strip_placeholder_lines(arr[:6])
            # 剥完占位词有页变空 → 视为解析失败，回退机械切割
            if all(pages):
                model_version = result["model_version"]
                cost = result["cost_cny"]
            else:
                pages = None
    except Exception:
        traceback.print_exc()
    if pages is None:
        pages = _split_pages(text, 6)
    async with SessionLocal() as session:
        for i, body in enumerate(pages, start=1):
            session.add(PageCopy(task_id=input_data["task_id"], page_index=i, body=body, claim_ids=[]))
        await session.commit()
    return {"page_count": len(pages), "model_version": model_version,
            "prompt_version": "page_split_llm_v1", "cost_cny": cost}


async def node_art_director(input_data: dict) -> dict:
    """视觉策划（2026-09-02 反模板化）：page_split 之后、asset_gen 之前为 6 页
    各出一份创意 brief 落 tasks.plan_json（迁移 017）。风格选定（ensure_task_style）
    也在此提前完成——方案需要风格描述作输入，asset_gen 直接读快照。策划失败
    不阻塞：plan_json 保持 NULL，asset_gen 回退固定构图/文字形式轮换。"""
    from src.models.tasks import Task
    from src.models.assets import Asset
    from src.models.drafts import PageCopy
    from src.services.style_pick import ensure_task_style
    from src.services.art_director import generate_plan, finalize_plan
    task_id = input_data["task_id"]
    async with SessionLocal() as session:
        task = (await session.execute(
            select(Task).where(Task.id == task_id))).scalar_one()
        query, mode, owner_id = task.query, task.mode or "general", task.created_by
        pages = (await session.execute(
            select(PageCopy).where(PageCopy.task_id == task_id)
            .order_by(PageCopy.page_index))).scalars().all()
        refs = await session.execute(
            select(Asset).where(Asset.task_id == task_id,
                                Asset.source_type == "official",
                                Asset.is_illustration == False))
        has_refs = any(a.image_url for a in refs.scalars())
    bodies = [(p.body or "") for p in pages][:6]
    gen_style, style_desc = await ensure_task_style(task_id, query, owner_id)
    # 驳回重跑：run_pipeline 把审核反馈注入 inputs，带给策划做定向调整
    feedback = (input_data.get("regen") or {}).get("feedback") or None
    if isinstance(feedback, list):
        feedback = "；".join(str(f) for f in feedback)
    # llm_call 显式走 nodes 模块级 failover：测试 patch 时策划同样走 mock
    plan = await generate_plan(query, mode, bodies, style_desc=style_desc,
                               has_refs=has_refs, feedback=feedback,
                               llm_call=lambda p: call_with_failover(
                                   p, DEEPSEEK_MODEL, KIMI_MODEL, max_retries=1))
    if not plan:
        return {"planned": False, "style": gen_style, "cost_cny": 0.0}
    cost = plan.get("cost_cny", 0.0)
    plan = finalize_plan(plan, style=gen_style,
                         no_text=settings.text_composite_enabled,
                         layout_offset=_layout_offset_for(task_id))
    async with SessionLocal() as session:
        t = (await session.execute(
            select(Task).where(Task.id == task_id))).scalar_one()
        t.plan_json = plan
        await session.commit()
    return {"planned": True, "style": gen_style, "cost_cny": cost,
            "model_version": plan.get("model")}


async def _generate_single_asset(task_id, page_index: int, prompt: str,
                                 reference_image_urls=None,
                                 provider: str = None) -> dict:
    from src.gateway.image_gen import generate_image
    r = await generate_image(prompt, reference_image_urls=reference_image_urls,
                             provider=provider)
    return {"task_id": task_id, "page_index": page_index, "hash": r["hash"],
            "image_url": r["image_url"],
            "source_type": "ai_generated", "copyright_status": "clear",
            "model_version": r["model_version"], "is_illustration": False}


async def _dedupe_and_validate(asset: dict, prompt: str, reference_urls,
                               task_id, page_index: int, seen_hashes: set,
                               page_body: str = "", provider: str = None,
                               layout_offset: int = 0, layout_bans=None,
                               composite_style: str = None) -> tuple:
    """内容级去重 + 尺寸校验：下载图片字节算内容 hash，与本任务已出图重复则换构图重生成一次；
    宽高比偏离 3:4 则在 model_version 上标记（交付导出时会统一归一到 1152x1536）。
    text_composite_enabled 且给了 page_body 时，AI 图只是无字背景：在本地化前把
    分页文案用真实字体合成上去（终极方案，从根上消除异体变形/伪汉字）。
    layout_offset 必须与 get_image_prompt 的页位偏移同值（合成落版对齐留白区）。
    composite_style（2026-09-02 老管线套图跟随）：显式版式名（classic_pills），
    跳过槽位轮换。返回 (asset, 额外生成次数)。"""
    import io
    from src.gateway.ocr import fetch_image_bytes
    extra = 0
    data = None
    try:
        data, ctype = await fetch_image_bytes(asset["image_url"])
        content_hash = hashlib.md5(data).hexdigest()
        if content_hash in seen_hashes:
            r2 = await _generate_single_asset(
                task_id, page_index, prompt + "（请换一种与之前不同的构图和视角）",
                reference_urls, provider=provider)
            extra += 1
            data2, ctype = await fetch_image_bytes(r2["image_url"])
            asset = r2
            data = data2
            content_hash = hashlib.md5(data).hexdigest()
        seen_hashes.add(content_hash)
        asset["hash"] = content_hash
        # 文字后期合成：把分页文案用真实字体合成到无字背景上（合成图固定 1152x1536）
        if settings.text_composite_enabled and (page_body or "").strip():
            from src.services.text_composite import composite_page
            composited = composite_page(data, page_body, page_index,
                                        offset=layout_offset, banned=layout_bans,
                                        style=composite_style)
            if composited:
                data, ctype = composited, "image/png"
                asset["hash"] = hashlib.md5(data).hexdigest()
                asset["model_version"] += "|textcmp"
        from PIL import Image
        w, h = Image.open(io.BytesIO(data)).size
        if abs(w / h - 0.75) > 0.05:
            asset["model_version"] += f"|badsize:{w}x{h}"
        # 立即本地化：上游生成 URL 会过期，落盘后 image_url 指向本地副本
        asset["origin_url"] = asset["image_url"]
        asset["image_url"] = _persist_image(task_id, page_index, "p", data, ctype)
    except Exception:
        # 下载/解析失败不阻塞出图（OCR 节点会再暴露问题），但必须留痕，
        # 否则去重/校验/持久化静默失效（2026-08-20 缺失 hashlib 导入的教训）
        traceback.print_exc()
    return asset, extra


async def _text_quality_gate(asset: dict, prompt: str, reference_urls,
                             task_id, page_index: int, expected_text: str,
                             seen_hashes: set, provider: str = None,
                             layout_offset: int = 0, layout_bans=None) -> tuple:
    """出图文字质检（2026-08-25 用户反馈「文字扭曲概率非常大」）：OCR 识别图上文字，
    与分页文案对撞，相似度低于阈值判为文字扭曲，换构图重生成
    （最多 settings.asset_text_max_attempts 次）；仍不达标在 model_version 打
    text_garble 标记，交人工审核定夺。OCR 服务异常不阻塞出图（视为未知、不重试，
    后续 ocr_read 节点会再暴露）。返回 (asset, 重生成次数, OCR 成本)。"""
    from src.gateway.ocr import ocr_image
    attempts = 0
    ocr_cost = 0.0
    if not (expected_text or "").strip():
        return asset, attempts, ocr_cost
    while True:
        try:
            ocr = await ocr_image(asset["image_url"])
            ocr_cost += ocr.get("cost_cny", 0.0)
            sim = text_similarity(expected_text, ocr["raw_text"])
        except Exception:
            traceback.print_exc()
            return asset, attempts, ocr_cost
        if sim >= settings.ocr_text_similarity_threshold:
            return asset, attempts, ocr_cost
        if attempts >= settings.asset_text_max_attempts:
            asset["model_version"] += f"|text_garble:{sim:.2f}"
            return asset, attempts, ocr_cost
        attempts += 1
        print(f"[asset_gen] 任务{task_id} 第{page_index}页文字相似度 {sim:.2f} "
              f"低于阈值 {settings.ocr_text_similarity_threshold}，第 {attempts} 次重生成")
        # 合成模式下 AI 图无字，OCR 对撞的是合成文字；不达标说明背景干扰文字区，
        # 重生成时要求更干净的留白；非合成模式则是 AI 文字本身扭曲
        retry_hint = ("（上一版背景干扰了文字区域：请保持预留区域干净、简洁、低细节，"
                      "画面中不要出现任何文字）" if settings.text_composite_enabled
                      else "（上一版图中文字扭曲/乱码：请进一步减少图中文字，"
                           "只保留大标题和最关键的一行字，确保每个汉字笔画正确）")
        regen = await _generate_single_asset(
            task_id, page_index, prompt + retry_hint, reference_urls,
            provider=provider)
        asset, dedupe_extra = await _dedupe_and_validate(
            regen, prompt, reference_urls, task_id, page_index, seen_hashes,
            page_body=expected_text, provider=provider,
            layout_offset=layout_offset, layout_bans=layout_bans)
        attempts += dedupe_extra


async def _vl_quality_gate(asset: dict, prompt: str, reference_urls,
                           task_id, page_index: int, page_body: str,
                           seen_hashes: set, ref_mode: bool,
                           provider: str = None, layout_offset: int = 0,
                           layout_bans=None) -> tuple:
    """VL 视觉二审（2026-09-01 借鉴 8003 ai_review）：OCR 文字关卡之后再查
    OCR 发现不了的问题——文字量过载/缺失、实景图嵌入不协调（大小/位置/对比度/
    遮挡主体）。不达标时把 VL 给的调整建议拼进提示词自动重生成（最多
    settings.vl_review_max_rounds 轮）；仍败在 model_version 打 |vl_flag 标记
    交人工审核。VL 调用失败默认通过不误杀。返回 (asset, 重生成次数, VL 成本)。"""
    from src.services.vl_review import vl_review_image
    extra = 0
    vl_cost = 0.0
    for rnd in range(1, settings.vl_review_max_rounds + 1):
        review = await vl_review_image(asset["image_url"], page_body,
                                       page_index, ref_mode)
        vl_cost += review.get("cost_cny", 0.0)
        if review["pass"]:
            return asset, extra, vl_cost
        flagged = review.get("issues") or ["视觉审核未通过"]
        if rnd >= settings.vl_review_max_rounds:
            asset["model_version"] += f"|vl_flag:{'；'.join(flagged)[:60]}"
            print(f"[asset_gen] 任务{task_id} 第{page_index}页 VL 审核 "
                  f"{rnd} 轮仍不达标：{'；'.join(flagged)[:80]}，打标交人工审核")
            return asset, extra, vl_cost
        adjust = review.get("suggest") or "精简图上文字，主体更突出，实景图嵌入更协调"
        print(f"[asset_gen] 任务{task_id} 第{page_index}页 VL 审核发现问题："
              f"{'；'.join(flagged)[:80]}，按建议重生成（第 {rnd} 轮）")
        regen_prompt = (prompt + f"（AI 视觉审核第{rnd}轮发现问题："
                        f"{'；'.join(flagged)[:80]}。调整要求：{adjust[:120]}）")
        regen = await _generate_single_asset(
            task_id, page_index, regen_prompt, reference_urls, provider=provider)
        extra += 1
        asset, dedupe_extra = await _dedupe_and_validate(
            regen, prompt, reference_urls, task_id, page_index, seen_hashes,
            page_body=page_body, provider=provider,
            layout_offset=layout_offset, layout_bans=layout_bans)
        extra += dedupe_extra
    return asset, extra, vl_cost


async def node_asset_gen(input_data: dict) -> dict:
    import asyncio
    from src.models.tasks import Task
    from src.models.assets import Asset
    from src.models.drafts import PageCopy
    from src.gateway.prompt_versions import get_image_prompt, get_effective_prompt
    async with SessionLocal() as session:
        task = (await session.execute(
            select(Task).where(Task.id == input_data["task_id"]))).scalar_one()
        mode = task.mode or "general"
        owner_id = task.created_by
        query = task.query
        # 任务级生图模型（2026-08-28）：NULL=全局默认 gpt-image-2，
        # 用户在「待生图」确认环节手动选择其它模型时才写入（迁移 010）
        img_provider = task.image_provider
        # 视觉策划方案（2026-09-02，迁移 017）：art_director 节点产出并冻结；
        # NULL=未策划/策划失败，下方 get_image_prompt 回退固定轮换
        plan_pages = None
        if task.plan_json and isinstance(task.plan_json, dict):
            plan_pages = {p.get("page"): p
                          for p in (task.plan_json.get("pages") or [])}
        # 版式禁用（2026-09-02，迁移 019）：{页码str: [禁用槽位]}，
        # 生图留白区与文字合成落版经 slot_for_page 顺延到未禁用槽位
        layout_bans = task.layout_bans or {}
        pages = await session.execute(
            select(PageCopy).where(PageCopy.task_id == input_data["task_id"]))
        page_list = pages.scalars().all()
        reference_urls = None
        pool_a = pool_b = common = None
        # 所有模式都取 official 实图作生图参考（2026-09-01 通用启用实景图）：
        # 有实图（搜索/手动上传/素材库复用）就融入画面，没有则回退纯 AI 生成
        refs = await session.execute(
            select(Asset).where(Asset.task_id == input_data["task_id"],
                                Asset.source_type == "official",
                                Asset.is_illustration == False))
        ref_assets = [a for a in refs.scalars() if a.image_url]
        reference_urls = [a.image_url for a in ref_assets]
        # compare 分主体参考图池（2026-08-25 反馈③④：每页双主体同框 + 多角度轮换）
        pool_a = [a.image_url for a in ref_assets
                  if (a.subject or "").startswith("A:")]
        pool_b = [a.image_url for a in ref_assets
                  if (a.subject or "").startswith("B:")]
        common = [a.image_url for a in ref_assets
                  if not (a.subject or "").startswith(("A:", "B:"))]

    def _page_refs(page_i: int):
        """每页参考图路由：compare 拆出 A/B 池时每页喂 A、B 各 2 张（按页码轮换，
        不同页拿到不同角度的参考图）+ 无标签公共图；拆不出主体时退回全量（旧行为）。"""
        if mode != "compare" or not (pool_a or pool_b):
            return reference_urls
        picks = []

        def _take(pool, n=2):
            if pool:
                for k in range(n):
                    u = pool[(page_i - 1 + k) % len(pool)]
                    if u not in picks:
                        picks.append(u)

        _take(pool_a)
        _take(pool_b)
        picks += [u for u in (common or []) if u not in picks]
        return picks or reference_urls
    # 生图视觉风格自适应（2026-08-28，迁移 011）：正文生成后、生图前为任务判定
    # 一次风格（用户风格库优先，空则内置 8 风格），6 张图共用；风格名落
    # tasks.gen_image_style，描述词注入提示词替换固定风格句。判定失败不阻塞
    # 出图（沿用模板默认风格句）。2026-09-02 起选定提前到 art_director 节点
    # （策划需要风格描述作输入），此处直接读快照；015 前老任务无快照按名反查。
    from src.services.style_pick import ensure_task_style
    gen_style, style_desc = await ensure_task_style(
        input_data["task_id"], query, owner_id)
    # 分页画面主体提取（2026-08-31 用户反馈「图文不对应」）：生图前用一次 LLM
    # 从 6 页文案提取每页具体画面主体注入提示词，替换通用主体锚定条款；提取
    # 失败回退 None，不阻塞出图（沿用通用锚定条款）。结果落 tasks.page_subjects
    # （迁移 014）便于排查。
    page_subjects = None
    try:
        from src.services.page_subject import extract_page_subjects
        bodies = [(p.body or "") for p in page_list][:6]
        while len(bodies) < 6:
            bodies.append("")
        page_subjects = await extract_page_subjects(
            bodies,
            llm_call=lambda p: call_with_failover(
                p, DEEPSEEK_MODEL, KIMI_MODEL, max_retries=1))
        if page_subjects:
            async with SessionLocal() as session:
                t = (await session.execute(
                    select(Task).where(Task.id == input_data["task_id"]))).scalar_one()
                t.page_subjects = page_subjects
                await session.commit()
    except Exception:
        traceback.print_exc()
        page_subjects = None
    # 自定义生图模板（提示词库启用的）替代系统模板；排版轮换仍由代码追加
    image_template = await get_effective_prompt("image_gen", mode, owner_id)
    no_text = settings.text_composite_enabled
    # 场景化视觉扩写（2026-09-02 移植 8003 实测有效的视觉导演层，迁移 020）：
    # 无字合成模式下，把 6 页文案扩写成英文视觉描述（含每题材自定色彩方向 +
    # 无字丰富度装置），gpt-image-2 对英文视觉指令理解更准。产出冻结到
    # tasks.visual_json（定点重生成沿用不重扩写）；失败回退中文无字骨架。
    visuals = None
    if no_text:
        try:
            from src.services.visual_writer import write_page_visuals
            bodies = [(p.body or "") for p in page_list][:6]
            while len(bodies) < 6:
                bodies.append("")
            visuals = await write_page_visuals(
                gen_style, style_desc, bodies,
                llm_call=lambda pr: call_with_failover(
                    pr, DEEPSEEK_MODEL, KIMI_MODEL, max_retries=1))
            if visuals:
                async with SessionLocal() as session:
                    t = (await session.execute(
                        select(Task).where(
                            Task.id == input_data["task_id"]))).scalar_one()
                    t.visual_json = visuals
                    await session.commit()
        except Exception:
            traceback.print_exc()
            visuals = None
    # 页位偏移（2026-09-02 反同质化）：布局/文字形式/合成槽位轮换起点随任务变
    layout_offset = _layout_offset_for(input_data["task_id"])

    def _bans_for(page_i: int):
        """该页被禁用的版式槽位（layout_bans 键为页码字符串）。"""
        return set(layout_bans.get(str(page_i)) or []) or None

    # 通用模式有实图时提示词前缀换「实景图融入」版（无实图回退纯 AI 版）
    general_has_refs = (mode == "general" and bool(reference_urls))

    def _prompt_for(i: int, body: str) -> str:
        return get_image_prompt(
            mode, body, i, template=image_template, no_text=no_text,
            style_desc=style_desc,
            page_subject=(page_subjects[i - 1]
                          if page_subjects and i <= 6 else None),
            has_refs=general_has_refs, layout_offset=layout_offset,
            layout_bans=_bans_for(i), plan_page=(plan_pages or {}).get(i),
            visual=(visuals["pages"][i - 1]
                    if visuals and i <= 6 else None),
            style_en=(visuals["style_en"] if visuals else None))

    prompts = [_prompt_for(i, p.body or "")
               for i, p in enumerate(page_list, start=1)]
    while len(prompts) < 6:
        prompts.append(_prompt_for(len(prompts) + 1, ""))
    # 生成策略（2026-09-01 借鉴 8003 并行出图）：image_gen_parallel>1 时 6 页
    # Semaphore 限流并发（去重/校验仍在页内串行，seen_hashes 由 asyncio 单线程
    # 协程共享、无竞态）；=1 保持串行+间隔旧行为（防测试账户限流）
    results = []
    seen_hashes = set()
    extra_gen = 0
    ocr_gate_cost = 0.0

    async def _gen_one_page(i: int) -> tuple:
        """生成一页：出图 → 去重/尺寸校验/文字合成 → OCR 文字关卡 → VL 视觉二审。
        返回 (asset, 额外生成次数, 审核成本)；mock 模式只出图不审核。"""
        refs_i = _page_refs(i) if reference_urls else None
        r = await _generate_single_asset(
            input_data["task_id"], i, prompts[i - 1], refs_i,
            provider=img_provider)
        if settings.mock_image_gen:
            return r, 0, 0.0
        page_body = page_list[i - 1].body if i - 1 < len(page_list) else ""
        r, extra = await _dedupe_and_validate(
            r, prompts[i - 1], refs_i, input_data["task_id"], i, seen_hashes,
            page_body=page_body or "", provider=img_provider,
            layout_offset=layout_offset, layout_bans=_bans_for(i))
        # 文字质检：OCR 对撞分页文案，扭曲图自动重生成（文字扭曲是最高频客诉）
        r, text_extra, gate_cost = await _text_quality_gate(
            r, prompts[i - 1], refs_i, input_data["task_id"], i,
            page_body or "", seen_hashes, provider=img_provider,
            layout_offset=layout_offset, layout_bans=_bans_for(i))
        extra += text_extra
        # VL 视觉二审（借鉴 8003）：查 OCR 发现不了的文字过载/实景嵌入不协调。
        # 省成本口径与 8003 一致：有实图的页（compare/single/通用带图）全审，
        # 纯 AI 页只审文字关卡已经出过问题的页
        if settings.vl_review_enabled and (reference_urls or text_extra > 0):
            r, vl_extra, vl_cost = await _vl_quality_gate(
                r, prompts[i - 1], refs_i, input_data["task_id"], i,
                page_body or "", seen_hashes, ref_mode=bool(refs_i),
                provider=img_provider, layout_offset=layout_offset,
                layout_bans=_bans_for(i))
            extra += vl_extra
            gate_cost += vl_cost
        return r, extra, gate_cost

    parallel = max(1, settings.image_gen_parallel)
    if parallel > 1 and not settings.mock_image_gen:
        sem = asyncio.Semaphore(parallel)

        async def _guarded(i: int) -> tuple:
            async with sem:
                return await _gen_one_page(i)

        page_results = await asyncio.gather(*(_guarded(i) for i in range(1, 7)))
        for r, extra, gate_cost in page_results:
            results.append(r)
            extra_gen += extra
            ocr_gate_cost += gate_cost
    else:
        for i in range(1, 7):
            r, extra, gate_cost = await _gen_one_page(i)
            results.append(r)
            extra_gen += extra
            ocr_gate_cost += gate_cost
            await asyncio.sleep(settings.image_gen_delay_seconds)
    async with SessionLocal() as session:
        for r in results:
            session.add(Asset(**r))
        await session.commit()
    from src.gateway.image_gen import cost_per_image
    return {"asset_count": len(results),
            "image_urls": [r.get("image_url") for r in results if r.get("image_url")],
            "cost_cny": 0 if settings.mock_image_gen
                        else (len(results) + extra_gen) * cost_per_image(img_provider)
                             + ocr_gate_cost}


async def node_ocr_read(input_data: dict) -> dict:
    from src.models.assets import OcrResult, Asset
    from src.gateway.ocr import ocr_image
    async with SessionLocal() as session:
        assets = await session.execute(
            select(Asset).where(Asset.task_id == input_data["task_id"],
                                Asset.source_type == "ai_generated",
                                Asset.is_active == True))
        rows = [(a.id, a.page_index, a.image_url) for a in assets.scalars()]
    if settings.mock_image_gen:
        # mock 生图时配图是占位 SVG，无真实文字可识别，沿用桩逻辑
        async with SessionLocal() as session:
            for asset_id, page_index, _ in rows:
                session.add(OcrResult(asset_id=asset_id, raw_text=f"page {page_index}",
                                      key_fields={"page": str(page_index)},
                                      confidence=0.95))
            await session.commit()
        return {"ocr_completed": True, "cost_cny": 0}
    results = []
    total_cost = 0.0
    for asset_id, page_index, image_url in rows:
        try:
            r = await ocr_image(image_url)
            results.append(OcrResult(asset_id=asset_id, raw_text=r["raw_text"],
                                     key_fields={"page": str(page_index),
                                                 "ocr_model": r["model"]},
                                     confidence=0.9))
            total_cost += r["cost_cny"]
        except Exception:
            # 单张识别失败不拖垮整条任务，置信度记 0 供交叉校验判风险
            results.append(OcrResult(asset_id=asset_id, raw_text="",
                                     key_fields={"page": str(page_index)},
                                     confidence=0.0))
    async with SessionLocal() as session:
        session.add_all(results)
        await session.commit()
    return {"ocr_completed": True, "cost_cny": total_cost}


async def node_cross_check(input_data: dict) -> dict:
    from src.models.assets import CrossCheck, OcrResult, Asset
    from src.models.drafts import PageCopy
    from src.quality.cross_check import extract_key_fields, compare_field
    async with SessionLocal() as session:
        pages = await session.execute(
            select(PageCopy).where(PageCopy.task_id == input_data["task_id"]))
        page_list = pages.scalars().all()
        # 每页配图的真实 OCR 文字（按 page_index 对齐）
        ocr_rows = await session.execute(
            select(Asset.page_index, OcrResult.raw_text, OcrResult.confidence)
            .join(OcrResult, OcrResult.asset_id == Asset.id)
            .where(Asset.task_id == input_data["task_id"],
                   Asset.source_type == "ai_generated",
                   Asset.is_active == True))
        ocr_map = {r.page_index: (r.raw_text or "", r.confidence)
                   for r in ocr_rows.all()}
        all_mismatches = []
        for p in page_list:
            expected = extract_key_fields(p.body)
            ocr_text, confidence = ocr_map.get(p.page_index, ("", 0.0))
            if confidence == 0.0:
                m = {"field_name": "ocr", "expected": "可识别",
                     "actual": "识别失败", "matched": False}
                session.add(CrossCheck(task_id=input_data["task_id"], **m))
                all_mismatches.append(m)
                continue
            actual = extract_key_fields(ocr_text)
            mismatches = compare_field(expected, actual)
            for m in mismatches:
                session.add(CrossCheck(task_id=input_data["task_id"], **m))
                all_mismatches.append(m)
        await session.commit()
    return {"mismatch_count": len(all_mismatches)}


async def node_risk_classify(input_data: dict) -> dict:
    from src.models.assets import CrossCheck
    from src.models.drafts import RuleResult
    from src.models.review import RiskClassification, Issue
    from src.models.entities import Claim, Evidence
    from src.risk.classifier import classify
    async with SessionLocal() as session:
        rules = await session.execute(select(RuleResult).where(RuleResult.task_id == input_data["task_id"]))
        checks = await session.execute(select(CrossCheck).where(CrossCheck.task_id == input_data["task_id"]))
        rule_list = [{"passed": r.passed, "rule_name": r.rule_name} for r in rules.scalars()]
        check_list = [{"matched": c.matched} for c in checks.scalars()]
        # 证据完整性：P0/P1 关键事实点必须有支撑证据，否则 evidence_complete=False
        evidence_complete = True
        claims = await session.execute(
            select(Claim).where(Claim.task_id == input_data["task_id"],
                                Claim.risk_level.in_(["P0", "P1"])))
        for claim in claims.scalars():
            ev = await session.execute(select(Evidence).where(Evidence.claim_id == claim.id))
            if ev.first() is None:
                evidence_complete = False
                break
        # P0 问题：存在未关闭的 P0 问题单即 has_p0_issue=True
        p0 = await session.execute(
            select(Issue).where(Issue.task_id == input_data["task_id"],
                                Issue.priority == "P0",
                                Issue.status == "open"))
        has_p0_issue = p0.first() is not None
        level, reasons = classify(rule_list, check_list, evidence_complete, has_p0_issue)
        rc = RiskClassification(task_id=input_data["task_id"], level=level, reasons=reasons)
        session.add(rc)
        await session.commit()
    return {"level": level, "reasons": reasons}


async def node_review_queue(input_data: dict) -> dict:
    from src.models.review import ReviewSession
    async with SessionLocal() as session:
        for role in ["A", "B", "C"]:
            session.add(ReviewSession(task_id=input_data["task_id"], role=role))
        await session.commit()
    return {"queued": ["A", "B", "C"]}


async def node_batch_signoff(input_data: dict) -> dict:
    from src.models.review import Batch
    async with SessionLocal() as session:
        b = Batch(risk_level="green", sampling_rate=0.20, member_count=1)
        session.add(b)
        await session.commit()
    return {"batch_id": str(b.id)}


async def node_publish_snapshot(input_data: dict) -> dict:
    from src.models.snapshots import PublishSnapshot
    from src.models.assets import Asset
    from src.models.review import Issue
    async with SessionLocal() as session:
        # 交付合同校验：页数=6、顺序、一页一图（缺页/错序/两图同页必驳回）
        # 只校验 AI 生成的交付配图；compare/single 的实景参考图（official）不计入交付页数
        assets = await session.execute(
            select(Asset).where(Asset.task_id == input_data["task_id"],
                                Asset.source_type == "ai_generated",
                                Asset.is_active == True)
            .order_by(Asset.page_index))
        asset_list = assets.scalars().all()
        delivery_errors = []
        page_indexes = [a.page_index for a in asset_list]
        if len(asset_list) != 6:
            delivery_errors.append(f"缺页或多余页：期望 6 页，实际 {len(asset_list)} 页")
        if sorted(page_indexes) != [1, 2, 3, 4, 5, 6]:
            delivery_errors.append(f"页序错误：{page_indexes}")
        if len(page_indexes) != len(set(page_indexes)):
            delivery_errors.append(f"两图同页：{page_indexes}")
        if delivery_errors:
            # 交付不合格转红色：写入 P0 问题单，不生成快照
            for err in delivery_errors:
                session.add(Issue(task_id=input_data["task_id"], role="B",
                                  priority="P0", description=err))
            await session.commit()
            return {"delivery_errors": delivery_errors, "snapshot_created": False}
        s = PublishSnapshot(task_id=input_data["task_id"], snapshot_data={"frozen": True})
        session.add(s)
        await session.commit()
    return {"snapshot_id": str(s.id), "delivery_errors": []}
