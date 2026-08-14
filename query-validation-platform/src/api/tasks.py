import asyncio
import csv
import io
import uuid
from fastapi import APIRouter, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import select, func, text
from src.db.session import SessionLocal
from src.models.tasks import Task

router = APIRouter()

# 后台流水线任务集合（防止被 GC）
_background_tasks = set()


@router.post("/api/tasks/import")
async def import_tasks(file: UploadFile = File(...)):
    content = await file.read()
    text = content.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    imported = 0
    errors = []
    async with SessionLocal() as session:
        for row in reader:
            try:
                key = f"{row['query']}|{row['content_type']}|{row.get('platform', '')}"
                existing = await session.execute(
                    select(Task).where(Task.idempotency_key == key))
                if existing.first():
                    continue
                session.add(Task(
                    idempotency_key=key,
                    query=row["query"],
                    content_type=row["content_type"],
                    platform=row.get("platform"),
                    status="draft",
                ))
                imported += 1
            except Exception as e:
                errors.append({"row": row, "error": str(e)})
        await session.commit()
    return {"imported": imported, "errors": errors}


class ImportQueriesIn(BaseModel):
    queries: list[str]        # 每行一个 Query
    content_type: str = "generic"   # 内容类型：generic / school / product / compare


@router.post("/api/tasks/import_queries")
async def import_queries(payload: ImportQueriesIn):
    """批量导入 Query 文本，创建任务并后台自动跑流水线。"""
    from src.pipeline.orchestrator import run_pipeline
    imported_ids = []
    async with SessionLocal() as session:
        for q in payload.queries:
            q = q.strip()
            if not q:
                continue
            key = f"{q}|{payload.content_type}|"
            existing = await session.execute(
                select(Task).where(Task.idempotency_key == key))
            if existing.first():
                continue
            task = Task(idempotency_key=key, query=q,
                        content_type=payload.content_type, status="draft")
            session.add(task)
            await session.flush()
            imported_ids.append(task.id)
        await session.commit()
    # 后台自动跑流水线（每个任务一个独立任务）
    for tid in imported_ids:
        t = asyncio.create_task(_run_and_mark(tid))
        _background_tasks.add(t)
        t.add_done_callback(_background_tasks.discard)
    return {"imported": len(imported_ids), "task_ids": [str(t) for t in imported_ids]}


async def _run_and_mark(task_id):
    from src.pipeline.orchestrator import run_pipeline
    try:
        async with SessionLocal() as session:
            task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one()
            task.status = "processing"
            await session.commit()
        await run_pipeline(task_id)
        async with SessionLocal() as session:
            task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one()
            task.status = "review"  # 流水线跑完，待审核
            await session.commit()
    except Exception:
        async with SessionLocal() as session:
            task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one()
            task.status = "failed"
            await session.commit()


@router.get("/api/tasks/stats")
async def task_stats():
    """任务状态统计 + 流水线进度（供进度页轮询）。"""
    async with SessionLocal() as session:
        status_counts = dict((await session.execute(
            select(Task.status, func.count(Task.id)).group_by(Task.status))).all())
        total = await session.execute(select(func.count(Task.id)))
        node_counts = dict((await session.execute(
            text("SELECT node_name, count(*) FROM node_events WHERE finished_at IS NOT NULL GROUP BY node_name"))).all())
        return {
            "total": total.scalar() or 0,
            "by_status": status_counts,
            "nodes_completed": node_counts,
        }
