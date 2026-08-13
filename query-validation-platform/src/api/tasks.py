import csv
import io
from fastapi import APIRouter, UploadFile, File
from sqlalchemy import select
from src.db.session import SessionLocal
from src.models.tasks import Task

router = APIRouter()


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
