import uuid
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select
from src.db.session import SessionLocal
from src.models.review import ReviewSession
from src.review.locks import acquire_lock

router = APIRouter()


class ClaimIn(BaseModel):
    task_id: str
    role: str
    reviewer_id: str


@router.post("/api/review/claim")
async def claim(payload: ClaimIn):
    return await acquire_lock(payload.task_id, payload.role, payload.reviewer_id)


@router.get("/api/review/queue/{role}")
async def queue(role: str):
    async with SessionLocal() as session:
        result = await session.execute(
            select(ReviewSession).where(
                ReviewSession.role == role,
                ReviewSession.finished_at.is_(None)))
        return {"sessions": [{"task_id": str(r.task_id)} for r in result.scalars()]}
