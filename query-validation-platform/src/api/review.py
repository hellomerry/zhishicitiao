import uuid
from datetime import datetime, timezone
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select
from src.db.session import SessionLocal
from src.models.review import ReviewSession, ReviewAction, Issue
from src.review.locks import acquire_lock
from src.review.heartbeat import record_heartbeat

router = APIRouter()


class ClaimIn(BaseModel):
    task_id: str
    role: str
    reviewer_id: str


@router.post("/api/review/claim")
async def claim(payload: ClaimIn):
    return await acquire_lock(payload.task_id, payload.role, payload.reviewer_id)


class HeartbeatIn(BaseModel):
    task_id: str
    role: str
    reviewer_id: str
    client_ts: datetime | None = None


@router.post("/api/review/heartbeat")
async def heartbeat(payload: HeartbeatIn):
    return await record_heartbeat(payload.task_id, payload.role, payload.reviewer_id, payload.client_ts)


class ActionIn(BaseModel):
    task_id: str
    role: str
    reviewer_id: str
    action_type: str  # approve / reject
    reason: str | None = None


@router.post("/api/review/action")
async def action(payload: ActionIn):
    async with SessionLocal() as session:
        rs = (await session.execute(
            select(ReviewSession).where(
                ReviewSession.task_id == uuid.UUID(payload.task_id),
                ReviewSession.role == payload.role,
                ReviewSession.finished_at.is_(None)))).scalars().first()
        if not rs:
            return {"ok": False, "error": "no active review session"}
        now = datetime.now(timezone.utc)
        session.add(ReviewAction(
            review_session_id=rs.id,
            idempotency_key=f"{rs.id}-{payload.action_type}-{uuid.uuid4().hex[:8]}",
            action_type=payload.action_type,
            client_ts=now, server_ts=now,
            payload={"reason": payload.reason or ""}))
        rs.finished_at = now
        if payload.action_type == "reject":
            session.add(Issue(task_id=uuid.UUID(payload.task_id), role=payload.role,
                              priority="P1", description=payload.reason or "审核驳回"))
        await session.commit()
    return {"ok": True, "action": payload.action_type}


@router.get("/api/review/queue/{role}")
async def queue(role: str):
    async with SessionLocal() as session:
        result = await session.execute(
            select(ReviewSession).where(
                ReviewSession.role == role,
                ReviewSession.finished_at.is_(None)))
        return {"sessions": [{"task_id": str(r.task_id)} for r in result.scalars()]}


@router.get("/api/review/task/{task_id}")
async def task_detail(task_id: str):
    """审核员查看的任务详情：正文、事实点、证据、图片、OCR、风险。"""
    from src.models.tasks import Task
    from src.models.drafts import Draft, PageCopy
    from src.models.entities import Claim, Evidence
    from src.models.assets import Asset, OcrResult
    from src.models.review import RiskClassification
    tid = uuid.UUID(task_id)
    async with SessionLocal() as session:
        task = (await session.execute(select(Task).where(Task.id == tid))).scalars().first()
        if not task:
            return {"error": "task not found"}
        draft = (await session.execute(
            select(Draft).where(Draft.task_id == tid).order_by(Draft.version.desc()))).scalars().first()
        claims = (await session.execute(select(Claim).where(Claim.task_id == tid))).scalars().all()
        evidences = []
        for c in claims:
            evs = (await session.execute(select(Evidence).where(Evidence.claim_id == c.id))).scalars().all()
            evidences.extend(evs)
        assets = (await session.execute(
            select(Asset).where(Asset.task_id == tid).order_by(Asset.page_index))).scalars().all()
        ocrs = (await session.execute(
            select(OcrResult).where(OcrResult.asset_id.in_([a.id for a in assets])))).scalars().all()
        risk = (await session.execute(
            select(RiskClassification).where(RiskClassification.task_id == tid))).scalars().first()
        return {
            "task": {"query": task.query, "content_type": task.content_type, "status": task.status},
            "draft": {"body": draft.body, "model_version": draft.model_version} if draft else None,
            "claims": [{"claim_text": c.claim_text, "risk_level": c.risk_level} for c in claims],
            "evidences": [{"source_url": e.source_url, "excerpt": e.excerpt} for e in evidences],
            "assets": [{"page_index": a.page_index, "source_type": a.source_type,
                        "image_url": a.image_url, "copyright_status": a.copyright_status} for a in assets],
            "ocr": [{"asset_id": str(o.asset_id), "raw_text": o.raw_text} for o in ocrs],
            "risk": {"level": risk.level, "reasons": risk.reasons} if risk else None,
        }
