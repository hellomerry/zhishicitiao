"""后台管理接口：工作周期状态、手动检修、导出记录、删除内容、重新开启。"""
import csv
import io
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, text

from src.db.session import SessionLocal
from src.models.tasks import Task
from src.stream.maintenance import cycle
from src.stream.progress import progress
from src.stream.scheduler import scheduler

router = APIRouter()

# 工作内容表（不含 users / organizations，删除时保留账号）
CONTENT_TABLES = [
    "tasks", "entity_snapshots", "claims", "evidence", "drafts", "page_copies",
    "assets", "ocr_results", "rule_results", "cross_checks",
    "risk_classifications", "review_sessions", "review_actions", "issues",
    "batches", "batch_members", "approvals", "publish_snapshots", "node_events",
]


@router.get("/api/admin/status")
async def status():
    async with SessionLocal() as session:
        total = await session.execute(select(func.count(Task.id)))
        by_status = dict((await session.execute(
            select(Task.status, func.count(Task.id)).group_by(Task.status))).all())
    return {
        "cycle": cycle.snapshot(),
        "scheduler": scheduler.snapshot(),
        "tasks": {"total": total.scalar() or 0, "by_status": by_status},
    }


@router.post("/api/admin/maintenance/start")
async def start_maintenance():
    return {"ok": True, "cycle": await cycle.enter_maintenance("manual")}


@router.post("/api/admin/maintenance/end")
async def end_maintenance():
    return {"ok": True, "cycle": await cycle.exit_maintenance("manual")}


@router.post("/api/admin/restart")
async def restart_work():
    """结束检修并重新开启工作任务。"""
    return {"ok": True, "cycle": await cycle.exit_maintenance("manual")}


@router.post("/api/admin/clear")
async def clear_work():
    """删除全部工作内容（保留账号），并清空队列内存状态。"""
    import asyncpg
    from src.config import settings
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    try:
        conn = await asyncpg.connect(dsn)
        try:
            # 锁超时 10s，避免在有任务执行时无限挂起
            await conn.execute("SET lock_timeout = '10s'")
            await conn.execute(f"TRUNCATE TABLE {', '.join(CONTENT_TABLES)} CASCADE")
        finally:
            await conn.close()
        scheduler.clear()
        progress.clear()
        return {"ok": True, "cleared_tables": len(CONTENT_TABLES)}
    except asyncpg.exceptions.LockNotAvailableError:
        return {"ok": False, "error": "仍有任务执行中，请等待任务完成后再删除"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@router.get("/api/admin/export")
async def export_records():
    """导出工作记录为 CSV（任务 + 正文长度 + 图/证据数 + 成本 + 风险）。"""
    from src.models.drafts import Draft
    from src.models.entities import Claim, Evidence
    from src.models.assets import Asset
    from src.models.events import NodeEvent
    from src.models.review import RiskClassification

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["task_id", "query", "content_type", "status", "risk_level",
                     "draft_chars", "assets", "evidence", "node_events", "cost_cny",
                     "model_version", "created_at"])

    async with SessionLocal() as session:
        tasks = (await session.execute(select(Task).order_by(Task.created_at))).scalars().all()
        for t in tasks:
            draft = (await session.execute(
                select(Draft).where(Draft.task_id == t.id)
                .order_by(Draft.version.desc()))).scalars().first()
            assets = (await session.execute(
                select(func.count(Asset.id)).where(Asset.task_id == t.id))).scalar() or 0
            evidence = (await session.execute(
                select(func.count(Evidence.id)).where(
                    Evidence.claim_id.in_(select(Claim.id).where(Claim.task_id == t.id))))).scalar() or 0
            node_count = (await session.execute(
                select(func.count(NodeEvent.id)).where(NodeEvent.task_id == t.id))).scalar() or 0
            cost = (await session.execute(
                select(func.coalesce(func.sum(NodeEvent.cost_estimate_cny), 0))
                .where(NodeEvent.task_id == t.id))).scalar() or 0
            risk = (await session.execute(
                select(RiskClassification).where(RiskClassification.task_id == t.id))).scalars().first()
            writer.writerow([
                str(t.id), t.query, t.content_type, t.status,
                risk.level if risk else "",
                len(draft.body) if draft else 0,
                assets, evidence, node_count,
                float(cost) if cost else 0,
                draft.model_version if draft else "",
                t.created_at.isoformat() if t.created_at else "",
            ])

    csv_bytes = ("\ufeff" + buf.getvalue()).encode("utf-8")
    filename = f"工作记录_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    from urllib.parse import quote
    content_disposition = (
        "attachment; filename=work_records.csv; "
        f"filename*=UTF-8''{quote(filename)}")
    return StreamingResponse(
        iter([csv_bytes]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": content_disposition})


@router.get("/api/admin/logs")
async def get_logs():
    """返回运行日志（内存缓冲，最近 3000 条）。"""
    return {"count": len(progress.log), "log": progress.get_log()}


@router.get("/api/admin/logs/download")
async def download_logs():
    """下载运行日志为文本文件。"""
    from urllib.parse import quote
    content = "\n".join(progress.get_log())
    text_bytes = ("\ufeff" + content).encode("utf-8")
    filename = f"运行日志_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    content_disposition = (
        "attachment; filename=run_log.txt; "
        f"filename*=UTF-8''{quote(filename)}")
    return StreamingResponse(
        iter([text_bytes]), media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": content_disposition})
