from datetime import datetime
from sqlalchemy import select
from src.db.session import SessionLocal
from src.gateway.failover import call_with_failover, DEEPSEEK_MODEL, KIMI_MODEL
from src.gateway.prompt_versions import get_prompt
from src.quality.rules import check_rules

NODES = [
    "task_import", "entity_bind", "evidence_build", "draft_gen",
    "rule_check", "page_split", "asset_gen", "ocr_read",
    "cross_check", "risk_classify", "review_queue",
    "batch_signoff", "publish_snapshot"
]


async def execute_node(task_id, node_name: str, input_data: dict, node_fn=None):
    from src.pipeline.idempotency import check_or_record_node_event
    async with SessionLocal() as session:
        event = await check_or_record_node_event(
            session, task_id, node_name, input_data)
        if event is None:
            return {"skipped": True}
        event.started_at = datetime.utcnow()
        try:
            if node_fn:
                output = await node_fn(input_data)
            else:
                output = {"node": node_name, "input": input_data}
            event.finished_at = datetime.utcnow()
            event.cost_estimate_cny = output.get("cost_cny", 0)
            event.model_version = output.get("model_version")
            event.prompt_version = output.get("prompt_version")
            await session.commit()
            return output
        except Exception as e:
            event.finished_at = datetime.utcnow()
            event.error_class = type(e).__name__
            event.retry_count = (event.retry_count or 0) + 1
            await session.commit()
            raise


async def _latest_draft_body(session, task_id):
    from src.models.drafts import Draft
    result = await session.execute(
        select(Draft).where(Draft.task_id == task_id).order_by(Draft.version.desc()))
    draft = result.scalars().first()
    return draft.body if draft else ""


async def node_draft_gen(input_data: dict) -> dict:
    from src.models.tasks import Task
    from src.models.drafts import Draft
    async with SessionLocal() as session:
        task = (await session.execute(
            select(Task).where(Task.id == input_data["task_id"]))).scalar_one()
        query = task.query
    prompt = get_prompt("draft", "v1") + "\n\n" + query
    result = await call_with_failover(prompt, DEEPSEEK_MODEL, KIMI_MODEL)
    async with SessionLocal() as session:
        session.add(Draft(
            task_id=input_data["task_id"], version=1, body=result["text"],
            model_version=result["model_version"], prompt_version="draft_v1"))
        await session.commit()
    return {"text": result["text"], "model_version": result["model_version"],
            "prompt_version": "draft_v1", "cost_cny": result["cost_cny"],
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
