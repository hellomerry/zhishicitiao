import hashlib
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
    "rule_check", "page_split", "asset_gen", "ocr_read",
    "cross_check", "risk_classify", "review_queue",
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
    """逐张下载候选图并本地化：达最低分辨率的全保留（不设上限）+ 排重，
    不足 min_keep 张时用次优图补齐保底（2026-08-27 用户要求：实图最少 6 张）。

    保留策略：无最高限制，只有最低限制（宽高 ≥ 阈值）+ 排重——URL 级 + 内容
    md5 级两级排重；seen 由调用方跨搜索组维护，避免同一张实图被不同搜索词
    重复收进。达标图不足 min_keep 时，依次用「下载成功但不达标」（按像素降序）
    和「下载失败」（沿用原地址，展示层走代理兜底）补齐到 min_keep。
    返回 (refs, filtered, dupes)，ref 含 url/origin/engine/hash（内容 md5）。
    """
    if seen is None:
        seen = {"hashes": set(), "urls": set()}
    good = []    # [(像素量, ref)] 达标
    lowres = []  # [(像素量或0, ref)] 下载成功但不达标（含读不出尺寸）
    failed = []  # [ref] 下载失败/空
    filtered = dupes = 0
    for it in candidates:
        url = it["image_url"]
        if url in seen["urls"]:
            dupes += 1
            continue
        seen["urls"].add(url)
        ref = {"url": url, "origin": None,
               "engine": it.get("engine", "search"),
               "hash": hashlib.md5(url.encode()).hexdigest()}
        try:
            data, ctype = await fetch_image_bytes(url)
        except Exception:
            filtered += 1
            failed.append(ref)
            continue
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


async def node_entity_bind(input_data: dict) -> dict:
    """搜实景图/实物图，存为 official 素材（compare/single 作参考图；general 跳过）。

    质量策略：搜索词带「高清」提高源头质量；下载后只按最低分辨率过滤 + 排重，
    达标的全部保留不设上限（2026-08-27 用户要求，mock 模式不下载不过滤，
    保留旧行为）。ref_filtered 记录被质量过滤淘汰的张数，ref_dupes 记录排重
    跳过的张数。
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
    if mode == "general":
        return {"searched_images": 0}
    # compare 模式拆主体 A/B 分搜（各带整体图 + 细节/侧面图，覆盖多角度对比），
    # 拆不出来时退回整词搜索（旧行为）；single 模式整词搜。
    groups = []  # [(tag, 搜索词, 抓取数)]
    if mode == "compare":
        pair = await _split_compare_subjects(query)
    else:
        pair = None
    if pair:
        for label, name in (("A", pair[0]), ("B", pair[1])):
            groups.append((f"{label}:{name}", f"{name} 高清", 6))
            groups.append((f"{label}:{name}（细节）", f"{name} 细节 侧面 高清", 4))
    else:
        groups.append((query, f"{query} 高清", 10))
    filtered_total = 0
    dupes_total = 0
    saved = 0
    # 跨搜索组共享排重集合：同一张实图不会被不同搜索词重复收进
    seen = {"hashes": set(), "urls": set()}
    async with SessionLocal() as session:
        for tag, q, cnt in groups:
            got = await search_image(q, count=cnt)
            if settings.mock_image_gen:
                refs = []
                for g in got:
                    if g["image_url"] in seen["urls"]:
                        dupes_total += 1
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
                    subject=tag, source_type="official", copyright_status="unknown",
                    hash=ref.get("hash") or hashlib.md5(
                        ref["url"].encode()).hexdigest(),
                    image_url=ref["url"], origin_url=ref["origin"],
                    model_version=ref["engine"],
                    is_illustration=False))
        await session.commit()
    return {"searched_images": saved, "ref_filtered": filtered_total,
            "ref_dupes": dupes_total,
            "subjects": list(pair) if pair else [],
            "cost_cny": settings.openserp_cost_per_call * len(groups)}


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
                                 source_level="P2", excerpt=(r["summary"] or "")[:500],
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
    prompt = template + "\n\n" + query
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


async def node_page_split(input_data: dict) -> dict:
    from src.models.drafts import PageCopy
    from src.models.tasks import Task
    from src.gateway.prompt_versions import get_effective_prompt
    async with SessionLocal() as session:
        text = await _latest_draft_body(session, input_data["task_id"])
        owner_id = (await session.execute(
            select(Task.created_by).where(Task.id == input_data["task_id"]))).scalar()
    # 首选 LLM 按页写图上文案；解析失败/调用失败退回机械切割（保证节点不卡死）
    pages, model_version, cost = None, "mechanical", 0.0
    try:
        import json as _json
        template = await get_effective_prompt("page_split", None, owner_id)
        llm_prompt = (template.replace("{body}", text) if "{body}" in template
                      else template + "\n\n" + text)
        result = await call_with_failover(llm_prompt, DEEPSEEK_MODEL, KIMI_MODEL)
        raw = result["text"].strip()
        if raw.startswith("```"):
            raw = raw.strip("`").lstrip("json").strip()
        arr = _json.loads(raw[raw.index("["):raw.rindex("]") + 1])
        arr = [str(p).strip() for p in arr if str(p).strip()]
        if len(arr) >= 6:
            pages = arr[:6]
            model_version = result["model_version"]
            cost = result["cost_cny"]
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
                               page_body: str = "", provider: str = None) -> tuple:
    """内容级去重 + 尺寸校验：下载图片字节算内容 hash，与本任务已出图重复则换构图重生成一次；
    宽高比偏离 3:4 则在 model_version 上标记（交付导出时会统一归一到 1152x1536）。
    text_composite_enabled 且给了 page_body 时，AI 图只是无字背景：在本地化前把
    分页文案用真实字体合成上去（终极方案，从根上消除异体变形/伪汉字）。
    返回 (asset, 额外生成次数)。"""
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
            composited = composite_page(data, page_body, page_index)
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
                             seen_hashes: set, provider: str = None) -> tuple:
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
            page_body=expected_text, provider=provider)
        attempts += dedupe_extra


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
        gen_style = task.gen_image_style
        # 任务级生图模型（2026-08-28）：NULL=全局默认 gpt-image-2，
        # 用户在「待生图」确认环节手动选择其它模型时才写入（迁移 010）
        img_provider = task.image_provider
        pages = await session.execute(
            select(PageCopy).where(PageCopy.task_id == input_data["task_id"]))
        page_list = pages.scalars().all()
        reference_urls = None
        pool_a = pool_b = common = None
        if mode in ("compare", "single"):
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
    # 出图（沿用模板默认风格句）。重跑/续跑时已判定的任务直接反查描述词。
    style_desc = None
    if not gen_style:
        try:
            from src.services.style_pick import pick_image_style
            async with SessionLocal() as session:
                draft_body = await _latest_draft_body(session, input_data["task_id"])
            gen_style, style_desc = await pick_image_style(
                query, draft_body, owner_id=owner_id,
                llm_call=lambda p: call_with_failover(
                    p, DEEPSEEK_MODEL, KIMI_MODEL, max_retries=1))
            async with SessionLocal() as session:
                t = (await session.execute(
                    select(Task).where(Task.id == input_data["task_id"]))).scalar_one()
                t.gen_image_style = gen_style
                await session.commit()
        except Exception:
            traceback.print_exc()
            gen_style = None
    if gen_style and style_desc is None:
        from src.services.style_pick import style_desc_for
        style_desc = await style_desc_for(gen_style, owner_id)
    # 自定义生图模板（提示词库启用的）替代系统模板；排版轮换仍由代码追加
    image_template = await get_effective_prompt("image_gen", mode, owner_id)
    no_text = settings.text_composite_enabled
    prompts = [get_image_prompt(mode, p.body or "", i, template=image_template,
                                no_text=no_text, style_desc=style_desc)
               for i, p in enumerate(page_list, start=1)]
    while len(prompts) < 6:
        prompts.append(get_image_prompt(mode, "", len(prompts) + 1,
                                        template=image_template, no_text=no_text,
                                        style_desc=style_desc))
    # 串行 + 间隔生成：避免测试账户限流，保证每张图有足够处理时间
    results = []
    seen_hashes = set()
    extra_gen = 0
    ocr_gate_cost = 0.0
    for i in range(1, 7):
        refs_i = _page_refs(i) if mode in ("compare", "single") else None
        r = await _generate_single_asset(
            input_data["task_id"], i, prompts[i - 1], refs_i,
            provider=img_provider)
        if not settings.mock_image_gen:
            page_body = page_list[i - 1].body if i - 1 < len(page_list) else ""
            r, extra = await _dedupe_and_validate(
                r, prompts[i - 1], refs_i, input_data["task_id"], i, seen_hashes,
                page_body=page_body or "", provider=img_provider)
            extra_gen += extra
            # 文字质检：OCR 对撞分页文案，扭曲图自动重生成（文字扭曲是最高频客诉）
            r, text_extra, gate_cost = await _text_quality_gate(
                r, prompts[i - 1], refs_i, input_data["task_id"], i,
                page_body or "", seen_hashes, provider=img_provider)
            extra_gen += text_extra
            ocr_gate_cost += gate_cost
        results.append(r)
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
