import uuid
from datetime import datetime, timezone
from sqlalchemy import select
from src.db.session import SessionLocal
from src.models.review import ReviewSession


async def acquire_lock(task_id: str, role: str, reviewer_id: str) -> dict:
    async with SessionLocal() as session:
        result = await session.execute(
            select(ReviewSession).where(
                ReviewSession.task_id == uuid.UUID(task_id),
                ReviewSession.role == role,
                ReviewSession.finished_at.is_(None)))
        sessions = result.scalars().all()
        now = datetime.now(timezone.utc)
        for s in sessions:
            if s.locked_at and s.last_heartbeat_at:
                if (now - s.last_heartbeat_at).total_seconds() < 30:
                    return {"acquired": False, "locked_by": str(s.reviewer_id)}
        if sessions:
            for s in sessions:
                s.reviewer_id = uuid.UUID(reviewer_id)
                s.locked_at = now
                s.last_heartbeat_at = now
                s.started_at = now
        else:
            session.add(ReviewSession(
                task_id=uuid.UUID(task_id), role=role,
                reviewer_id=uuid.UUID(reviewer_id),
                locked_at=now, last_heartbeat_at=now, started_at=now))
        await session.commit()
    return {"acquired": True}
