"""任务归属隔离（2026-08-26）：非 admin 只能看到/操作自己创建的任务。

口径（用户确认）：
- 任务列表/详情/回收站/导出按 `tasks.created_by` 隔离；admin 不受限。
- 审核队列 /api/review/* 全员可见（审核员的职责就是审别人的任务）；
  任务详情因此对非属主放行 status=="review" 的任务（与队列可见性一致）。
- 彻底删除（purge）已是「admin 或 admin 密码」，不按归属再限制。
- 实时监控/随机抽查为运营视图，不隔离；stats 传非 admin actor 时按归属过滤计数。
- 历史任务（created_by 为空）由迁移 008 回填为 admin 所有。
"""
from fastapi import HTTPException
from sqlalchemy import text


async def get_actor(session, actor: str):
    """在职用户校验，返回 (uid, role)；未知/停用 → 401。与 trash/prompts 同口径。"""
    row = (await session.execute(
        text("SELECT id, role FROM users WHERE name = :n AND active"),
        {"n": actor})).first()
    if not row:
        raise HTTPException(status_code=401, detail=f"用户不存在: {actor}")
    return row[0], row[1]


async def find_user_id(session, actor: str):
    """软解析：actor 是在职用户则返回 uid，否则 None（导入等不强制鉴权的场景用）。"""
    if not actor:
        return None
    return (await session.execute(
        text("SELECT id FROM users WHERE name = :n AND active"),
        {"n": actor})).scalar_one_or_none()


def owner_filter(task_cls, uid, role) -> list:
    """归属过滤条件：admin 不加过滤；其余只看自己创建的（含回填为 admin 的历史任务）。"""
    return [] if role == "admin" else [task_cls.created_by == uid]


def check_owner(task, uid, role, allow_review: bool = False) -> None:
    """非属主且非 admin → 404（不泄露任务是否存在）。
    allow_review=True 时放行审核中的任务（与审核队列全员可见口径一致）。"""
    if role == "admin":
        return
    if task.created_by is not None and task.created_by == uid:
        return
    if allow_review and task.status == "review":
        return
    raise HTTPException(status_code=404, detail="任务不存在")
