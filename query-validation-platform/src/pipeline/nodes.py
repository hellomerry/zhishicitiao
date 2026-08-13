from datetime import datetime
from src.db.session import SessionLocal
from src.gateway.failover import call_with_failover, DEEPSEEK_MODEL, KIMI_MODEL
from src.gateway.prompt_versions import get_prompt

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


async def node_draft_gen(input_data: dict) -> dict:
    prompt = get_prompt("draft", "v1") + "\n\n" + input_data["query"]
    result = await call_with_failover(prompt, DEEPSEEK_MODEL, KIMI_MODEL)
    return {"text": result["text"], "model_version": result["model_version"],
            "prompt_version": "draft_v1", "cost_cny": result["cost_cny"],
            "degraded": result["degraded"]}
