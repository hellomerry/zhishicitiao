"""回收站（2026-08-26）：任务软删除 + 恢复 + admin 彻底删除。

设计：status="trashed" 即入站（原状态存 prev_status），现有按状态过滤的查询
（审核队列/导出/抽查/调度恢复/retry）全部自动排除回收站任务，无需逐个改。
仅终态任务（review/approved/rejected/failed）可移入——draft 在队列、
processing 在生成，移入会与调度器状态打架。
彻底删除（purge）仅 admin：清全部关联表 + tasks 行 + static/generated 磁盘文件；
activity_logs 保留（审计）。
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select, text

from src.db.session import SessionLocal
from src.models.tasks import Task
from src.pipeline.nodes import GENERATED_DIR
from src.services.activity import log_action
from src.services.regen import clear_generated_content

router = APIRouter()

_TRASHABLE = ("review", "approved", "rejected", "failed")


async def _actor_role(session, actor: str):
    """操作人鉴权：须为在职用户；返回 (uid, role)。与 prompts.py 的 _actor 同口径。"""
    row = (await session.execute(
        text("SELECT id, role FROM users WHERE name = :n AND active"),
        {"n": actor})).first()
    if not row:
        raise HTTPException(status_code=401, detail=f"用户不存在: {actor}")
    return row[0], row[1]


def _row(t: Task) -> dict:
    return {"id": str(t.id), "query": t.query, "mode": t.mode,
            "prev_status": t.prev_status,
            "trashed_by": t.trashed_by,
            "trashed_at": t.trashed_at.isoformat() if t.trashed_at else None,
            "created_at": t.created_at.isoformat() if t.created_at else None}


@router.get("/api/trash")
async def list_trash(limit: int = 50, offset: int = 0):
    """回收站列表（所有登录用户可见），按移入时间倒序。"""
    limit = max(1, min(limit, 200))
    async with SessionLocal() as session:
        where = [Task.status == "trashed"]
        total = (await session.execute(
            select(func.count(Task.id)).where(*where))).scalar() or 0
        tasks = (await session.execute(
            select(Task).where(*where)
            .order_by(Task.trashed_at.desc()).limit(limit).offset(offset))).scalars().all()
    return {"total": total, "items": [_row(t) for t in tasks]}


@router.post("/api/tasks/{task_id}/trash")
async def trash_task(task_id: str, actor: str = "anonymous"):
    """移入回收站（软删除，可恢复）。仅终态任务可移入。"""
    tid = uuid.UUID(task_id)
    async with SessionLocal() as session:
        task = (await session.execute(
            select(Task).where(Task.id == tid))).scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task.status == "trashed":
            return {"ok": True, "already": True}
        if task.status not in _TRASHABLE:
            raise HTTPException(
                status_code=409,
                detail=f"任务当前状态为「{task.status}」，排队/生成中的任务不能移入回收站")
        await _actor_role(session, actor)
        prev = task.status
        query = task.query
        task.prev_status = prev
        task.status = "trashed"
        task.trashed_at = datetime.now(timezone.utc)
        task.trashed_by = actor
        await session.commit()
    await log_action(actor, "trash_task", f"移入回收站（原状态 {prev}）：{query[:50]}",
                     task_id=tid)
    return {"ok": True, "prev_status": prev}


@router.post("/api/tasks/{task_id}/restore")
async def restore_task(task_id: str, actor: str = "anonymous"):
    """从回收站恢复：状态还原为移入前的状态。"""
    tid = uuid.UUID(task_id)
    async with SessionLocal() as session:
        task = (await session.execute(
            select(Task).where(Task.id == tid))).scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task.status != "trashed":
            raise HTTPException(status_code=409, detail="任务不在回收站中")
        await _actor_role(session, actor)
        restored = task.prev_status or "review"
        query = task.query
        task.status = restored
        task.prev_status = None
        task.trashed_at = None
        task.trashed_by = None
        await session.commit()
    await log_action(actor, "restore_task", f"从回收站恢复（还原为 {restored}）：{query[:50]}",
                     task_id=tid)
    return {"ok": True, "status": restored}


class _BatchIds(BaseModel):
    task_ids: list[str]
    actor: str = "anonymous"


def _parse_ids(raw: list[str]) -> list[uuid.UUID]:
    ids = []
    for s in raw[:500]:  # 单次批量上限 500 条
        try:
            ids.append(uuid.UUID(s))
        except (ValueError, AttributeError):
            continue
    return ids


@router.post("/api/tasks/trash_batch")
async def trash_batch(body: _BatchIds):
    """批量移入回收站：终态任务移入，排队/生成中/已入站的跳过。返回 {moved, skipped}。"""
    ids = _parse_ids(body.task_ids)
    if not ids:
        raise HTTPException(status_code=400, detail="task_ids 为空")
    async with SessionLocal() as session:
        await _actor_role(session, body.actor)
        tasks = (await session.execute(
            select(Task).where(Task.id.in_(ids)))).scalars().all()
        now = datetime.now(timezone.utc)
        moved = 0
        for t in tasks:
            if t.status not in _TRASHABLE:
                continue
            t.prev_status = t.status
            t.status = "trashed"
            t.trashed_at = now
            t.trashed_by = body.actor
            moved += 1
        await session.commit()
    skipped = len(ids) - moved
    if moved:
        await log_action(body.actor, "trash_task", f"批量移入回收站 {moved} 条任务")
    return {"ok": True, "moved": moved, "skipped": skipped}


@router.post("/api/tasks/restore_batch")
async def restore_batch(body: _BatchIds):
    """批量恢复：仅在回收站中的任务还原为移入前状态。返回 {restored, skipped}。"""
    ids = _parse_ids(body.task_ids)
    if not ids:
        raise HTTPException(status_code=400, detail="task_ids 为空")
    async with SessionLocal() as session:
        await _actor_role(session, body.actor)
        tasks = (await session.execute(
            select(Task).where(Task.id.in_(ids)),
        )).scalars().all()
        restored = 0
        for t in tasks:
            if t.status != "trashed":
                continue
            t.status = t.prev_status or "review"
            t.prev_status = None
            t.trashed_at = None
            t.trashed_by = None
            restored += 1
        await session.commit()
    skipped = len(ids) - restored
    if restored:
        await log_action(body.actor, "restore_task", f"批量从回收站恢复 {restored} 条任务")
    return {"ok": True, "restored": restored, "skipped": skipped}


@router.delete("/api/tasks/{task_id}/purge")
async def purge_task(task_id: str, actor: str = "anonymous"):
    """彻底删除（仅 admin）：清全部关联表 + tasks 行 + static/generated 磁盘文件。
    不可恢复；activity_logs 保留。"""
    from src.models.events import NodeEvent
    from src.models.review import (Approval, BatchMember, Issue, RejectMark,
                                   ReviewAction, ReviewSession)
    tid = uuid.UUID(task_id)
    async with SessionLocal() as session:
        _, role = await _actor_role(session, actor)
        if role != "admin":
            raise HTTPException(status_code=403, detail="仅管理员可彻底删除")
        task = (await session.execute(
            select(Task).where(Task.id == tid))).scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task.status != "trashed":
            raise HTTPException(status_code=409,
                                detail="任务不在回收站中，请先移入回收站再彻底删除")
        query = task.query
        # 内容产物（草稿/分页/配图/OCR/校验/风险/证据/快照 + 未完成审核会话）
        await clear_generated_content(session, tid)
        # 审核历史与节点事件（regen 时保留，彻底删除时全清）
        session_ids = (await session.execute(
            select(ReviewSession.id).where(ReviewSession.task_id == tid))).scalars().all()
        if session_ids:
            await session.execute(
                delete(ReviewAction).where(ReviewAction.review_session_id.in_(session_ids)))
        await session.execute(delete(ReviewSession).where(ReviewSession.task_id == tid))
        await session.execute(delete(Approval).where(Approval.task_id == tid))
        await session.execute(delete(Issue).where(Issue.task_id == tid))
        await session.execute(delete(RejectMark).where(RejectMark.task_id == tid))
        await session.execute(delete(BatchMember).where(BatchMember.task_id == tid))
        await session.execute(delete(NodeEvent).where(NodeEvent.task_id == tid))
        await session.execute(delete(Task).where(Task.id == tid))
        await session.commit()
    # 磁盘文件（命名前缀即 task_id，见 nodes._persist_image）
    deleted_files = 0
    if GENERATED_DIR.exists():
        for f in GENERATED_DIR.glob(f"{task_id}_*"):
            try:
                f.unlink()
                deleted_files += 1
            except OSError:
                pass
    await log_action(actor, "purge_task",
                     f"彻底删除任务：{query[:50]}（含 {deleted_files} 个磁盘文件）")
    return {"ok": True, "deleted_files": deleted_files}
