import hashlib
import json
from datetime import datetime
from sqlalchemy import select
from src.models.events import NodeEvent


def compute_node_key(task_id: str, node_name: str, payload: dict) -> str:
    payload_str = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    raw = f"{task_id}|{node_name}|{payload_str}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def check_or_record_node_event(session, task_id, node_name: str,
                                     payload: dict, model_version: str = None,
                                     prompt_version: str = None):
    key = compute_node_key(str(task_id), node_name, payload)
    existing = await session.execute(
        select(NodeEvent).where(NodeEvent.node_idempotency_key == key))
    row = existing.first()
    if row is not None:
        return None  # already done, idempotent skip
    event = NodeEvent(
        task_id=task_id, node_name=node_name, node_idempotency_key=key,
        enqueued_at=datetime.utcnow(),
        model_version=model_version, prompt_version=prompt_version,
    )
    session.add(event)
    await session.flush()
    return event
