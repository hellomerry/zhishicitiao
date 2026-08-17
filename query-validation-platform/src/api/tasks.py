import csv
import io
import uuid
from fastapi import APIRouter, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import select, func, text
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.stream.scheduler import scheduler

router = APIRouter()


@router.post("/api/tasks/import")
async def import_tasks(file: UploadFile = File(...)):
    content = await file.read()
    text_content = content.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text_content))
    imported = 0
    errors = []
    enqueued = []
    async with SessionLocal() as session:
        for row in reader:
            try:
                mode = (row.get("mode") or "general").strip()
                key = f"{row['query']}|{row['content_type']}|{row.get('platform', '')}|{mode}"
                existing = await session.execute(
                    select(Task).where(Task.idempotency_key == key))
                if existing.first():
                    continue
                task = Task(
                    idempotency_key=key,
                    query=row["query"],
                    content_type=row["content_type"],
                    platform=row.get("platform"),
                    mode=mode,
                    status="draft",
                )
                session.add(task)
                await session.flush()
                enqueued.append((task.id, row["query"]))
                imported += 1
            except Exception as e:
                errors.append({"row": row, "error": str(e)})
        await session.commit()
    for tid, q in enqueued:
        await scheduler.enqueue(tid, q)
    return {"imported": imported, "errors": errors}


class ImportQueriesIn(BaseModel):
    queries: list[str]        # 每行一个 Query
    content_type: str = "generic"   # 内容类型：generic / school / product / compare
    mode: str = "general"     # 生产模式：compare / single / general


@router.post("/api/tasks/import_queries")
async def import_queries(payload: ImportQueriesIn):
    """批量导入 Query 文本，创建任务并加入队列（按并发限制排队执行）。"""
    imported = []
    async with SessionLocal() as session:
        for q in payload.queries:
            q = q.strip()
            if not q:
                continue
            key = f"{q}|{payload.content_type}|{payload.mode}"
            existing = await session.execute(
                select(Task).where(Task.idempotency_key == key))
            if existing.first():
                continue
            task = Task(idempotency_key=key, query=q,
                        content_type=payload.content_type, mode=payload.mode,
                        status="draft")
            session.add(task)
            await session.flush()
            imported.append((task.id, q))
        await session.commit()
    for tid, q in imported:
        await scheduler.enqueue(tid, q)
    return {"imported": len(imported), "task_ids": [str(t) for t, _ in imported],
            "queued": True, "queue_size": scheduler.queue.qsize(),
            "concurrency": scheduler.limiter.capacity}


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
            "queue": scheduler.snapshot(),
        }


@router.get("/api/tasks/random_sample")
async def random_sample():
    """随机抽查一条已完成内容：正文 + 6 图 + 证据 + 风险。"""
    from src.models.drafts import Draft, PageCopy
    from src.models.entities import Claim, Evidence
    from src.models.assets import Asset
    from src.models.review import RiskClassification
    async with SessionLocal() as session:
        done = await session.execute(
            select(Task).where(Task.status.in_(["review", "approved"]))
            .order_by(func.random()).limit(1))
        task = done.scalars().first()
        if not task:
            return {"ok": False, "error": "暂无已完成内容"}
        tid = task.id
        draft = (await session.execute(
            select(Draft).where(Draft.task_id == tid).order_by(Draft.version.desc()))).scalars().first()
        assets = (await session.execute(
            select(Asset).where(Asset.task_id == tid).order_by(Asset.page_index))).scalars().all()
        claims = (await session.execute(select(Claim).where(Claim.task_id == tid))).scalars().all()
        evidences = []
        for c in claims:
            evs = (await session.execute(select(Evidence).where(Evidence.claim_id == c.id))).scalars().all()
            evidences.extend(evs)
        risk = (await session.execute(
            select(RiskClassification).where(RiskClassification.task_id == tid))).scalars().first()
        return {
            "ok": True,
            "task_id": str(tid),
            "query": task.query,
            "content_type": task.content_type,
            "status": task.status,
            "draft": {"body": draft.body, "model_version": draft.model_version} if draft else None,
            "assets": [{"page_index": a.page_index, "source_type": a.source_type,
                        "image_url": a.image_url, "copyright_status": a.copyright_status} for a in assets],
            "claims": [{"claim_text": c.claim_text, "risk_level": c.risk_level} for c in claims],
            "evidences": [{"source_url": e.source_url, "excerpt": e.excerpt} for e in evidences],
            "risk": {"level": risk.level, "reasons": risk.reasons} if risk else None,
        }
