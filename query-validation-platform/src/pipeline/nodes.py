import traceback
from datetime import datetime, timezone
from sqlalchemy import select
from src.db.session import SessionLocal
from src.gateway.failover import call_with_failover, DEEPSEEK_MODEL, KIMI_MODEL
from src.quality.rules import check_rules

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


async def node_entity_bind(input_data: dict) -> dict:
    """搜实景图/实物图，存为 official 素材（compare/single 作参考图；general 跳过）。"""
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
    images = await search_image(query, count=6)
    async with SessionLocal() as session:
        for i, img in enumerate(images, start=1):
            session.add(Asset(
                task_id=input_data["task_id"], page_index=i,
                subject=query, source_type="official", copyright_status="unknown",
                hash=hashlib.md5(img["image_url"].encode()).hexdigest(),
                image_url=img["image_url"], model_version=img.get("engine", "search"),
                is_illustration=False))
        await session.commit()
    return {"searched_images": len(images)}


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
    deepseek_text = await deepseek_verify(query)
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
            "conflicts": conflicts}


async def node_draft_gen(input_data: dict) -> dict:
    from src.models.tasks import Task
    from src.models.drafts import Draft
    from src.gateway.prompt_versions import get_draft_prompt
    async with SessionLocal() as session:
        task = (await session.execute(
            select(Task).where(Task.id == input_data["task_id"]))).scalar_one()
        query = task.query
        mode = task.mode or "general"
    prompt = get_draft_prompt(mode) + "\n\n" + query
    prompt_version = f"draft_{mode}_v1"
    result = await call_with_failover(prompt, DEEPSEEK_MODEL, KIMI_MODEL)
    async with SessionLocal() as session:
        session.add(Draft(
            task_id=input_data["task_id"], version=1, body=result["text"],
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


async def node_page_split(input_data: dict) -> dict:
    from src.models.drafts import PageCopy
    async with SessionLocal() as session:
        text = await _latest_draft_body(session, input_data["task_id"])
    chunk_size = max(1, 350 // 6)
    pages = [text[i:i + chunk_size] for i in range(0, min(len(text), 350), chunk_size)]
    while len(pages) < 6:
        pages.append("")
    async with SessionLocal() as session:
        for i, body in enumerate(pages[:6], start=1):
            session.add(PageCopy(task_id=input_data["task_id"], page_index=i, body=body, claim_ids=[]))
        await session.commit()
    return {"page_count": min(len(pages), 6)}


async def _generate_single_asset(task_id, page_index: int, prompt: str,
                                 reference_image_urls=None) -> dict:
    from src.gateway.image_gen import generate_image
    r = await generate_image(prompt, reference_image_urls=reference_image_urls)
    return {"task_id": task_id, "page_index": page_index, "hash": r["hash"],
            "image_url": r["image_url"],
            "source_type": "ai_generated", "copyright_status": "clear",
            "model_version": r["model_version"], "is_illustration": False}


async def node_asset_gen(input_data: dict) -> dict:
    import asyncio
    from src.models.tasks import Task
    from src.models.assets import Asset
    from src.models.drafts import PageCopy
    from src.gateway.prompt_versions import get_image_prompt
    async with SessionLocal() as session:
        task = (await session.execute(
            select(Task).where(Task.id == input_data["task_id"]))).scalar_one()
        mode = task.mode or "general"
        pages = await session.execute(
            select(PageCopy).where(PageCopy.task_id == input_data["task_id"]))
        page_list = pages.scalars().all()
        reference_urls = None
        if mode in ("compare", "single"):
            refs = await session.execute(
                select(Asset).where(Asset.task_id == input_data["task_id"],
                                    Asset.source_type == "official",
                                    Asset.is_illustration == False))
            reference_urls = [a.image_url for a in refs.scalars() if a.image_url]
    prompts = [get_image_prompt(mode, (p.body or "")[:200]) for p in page_list]
    while len(prompts) < 6:
        prompts.append(get_image_prompt(mode, ""))
    sem = asyncio.Semaphore(2)
    async def _gen(i, prompt):
        async with sem:
            return await _generate_single_asset(input_data["task_id"], i, prompt,
                                                reference_urls)
    results = await asyncio.gather(
        *[_gen(i, prompts[i - 1]) for i in range(1, 7)])
    async with SessionLocal() as session:
        for r in results:
            session.add(Asset(**r))
        await session.commit()
    return {"asset_count": len(results),
            "image_urls": [r.get("image_url") for r in results if r.get("image_url")]}


async def node_ocr_read(input_data: dict) -> dict:
    from src.models.assets import OcrResult, Asset
    async with SessionLocal() as session:
        assets = await session.execute(
            select(Asset).where(Asset.task_id == input_data["task_id"]))
        for asset in assets.scalars():
            session.add(OcrResult(asset_id=asset.id, raw_text=f"page {asset.page_index}",
                                  key_fields={"page": str(asset.page_index)},
                                  confidence=0.95))
        await session.commit()
    return {"ocr_completed": True}


async def node_cross_check(input_data: dict) -> dict:
    from src.models.assets import CrossCheck
    from src.models.drafts import PageCopy
    from src.quality.cross_check import extract_key_fields, compare_field
    async with SessionLocal() as session:
        pages = await session.execute(
            select(PageCopy).where(PageCopy.task_id == input_data["task_id"]))
        page_list = pages.scalars().all()
        all_mismatches = []
        for p in page_list:
            expected = extract_key_fields(p.body)
            actual = {"page": str(p.page_index)}
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
        assets = await session.execute(
            select(Asset).where(Asset.task_id == input_data["task_id"]).order_by(Asset.page_index))
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
