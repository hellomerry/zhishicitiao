import uuid
import pytest
from sqlalchemy import select
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.models.events import NodeEvent
from src.pipeline.orchestrator import run_pipeline


@pytest.mark.asyncio
async def test_pipeline_runs_first_three_nodes():
    async with SessionLocal() as session:
        task = Task(idempotency_key=f"test-{uuid.uuid4().hex[:8]}", query="test", content_type="x")
        session.add(task)
        await session.commit()
        await session.refresh(task)
        task_id = task.id
    results = await run_pipeline(task_id)
    assert len(results) == 3
    assert results[0]["node"] == "task_import"
    async with SessionLocal() as session:
        events = await session.execute(
            select(NodeEvent).where(NodeEvent.task_id == task_id))
        assert len(events.scalars().all()) == 3
