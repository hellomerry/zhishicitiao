from sqlalchemy import Column, Integer, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import declarative_base
import uuid
from datetime import datetime, timezone

Base = declarative_base()


class Task(Base):
    __tablename__ = "tasks"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    idempotency_key = Column(Text, unique=True, nullable=False)
    query = Column(Text, nullable=False)
    content_type = Column(Text, nullable=False)
    mode = Column(Text, nullable=False, default="general")
    platform = Column(Text)
    sla_hours = Column(Integer, nullable=False, default=24)
    priority = Column(Text, nullable=False, default="normal")
    status = Column(Text, nullable=False, default="draft")
    template_id = Column(UUID(as_uuid=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    created_by = Column(UUID(as_uuid=True))
    # 任务级生图模型（2026-08-28，迁移 010）：NULL=全局默认（gpt-image-2）；
    # 用户在「待生图」确认环节手动选择其它模型（gemini 等）时才生效
    image_provider = Column(Text)
    # 任务级生图视觉风格（2026-08-28，迁移 011）：asset_gen 生图前按 query+正文
    # 自动判定一次（用户风格库优先，空则内置 8 风格），6 张图共用；NULL=未判定
    gen_image_style = Column(Text)
    # 风格描述快照（2026-08-31，迁移 015）：选定风格时冻结描述词在任务上，
    # 风格库后续编辑/删除不影响本任务重出图；NULL=未快照（015 前老任务按名反查）
    gen_image_style_desc = Column(Text)
    # 分页画面主体（2026-08-31，迁移 014）：asset_gen 从 6 页分页文案 LLM 提取
    # 的每页画面主体（JSON 数组），注入生图提示词用；NULL=未提取/提取失败
    page_subjects = Column(JSONB)
    # 视觉策划方案（2026-09-02，迁移 017）：art_director 为 6 页各出一份创意
    # brief（JSON：pages 数组 + model/created_at），confirm_gen 环节人工确认/
    # 编辑/重策划；注入生图提示词替代固定构图/文字形式轮换。NULL=未策划/失败
    plan_json = Column(JSONB)
    # 回收站（2026-08-26）：status="trashed" 时记录原状态/时间/操作人，恢复用
    prev_status = Column(Text)
    trashed_at = Column(TIMESTAMP(timezone=True))
    trashed_by = Column(Text)
