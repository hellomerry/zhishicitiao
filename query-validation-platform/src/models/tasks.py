from sqlalchemy import Column, Integer, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
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
    # 回收站（2026-08-26）：status="trashed" 时记录原状态/时间/操作人，恢复用
    prev_status = Column(Text)
    trashed_at = Column(TIMESTAMP(timezone=True))
    trashed_by = Column(Text)
