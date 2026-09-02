"""风格关键词库模型（迁移 011）：风格 → 关键词/描述词 的可训练映射。"""
from sqlalchemy import Boolean, Column, Text, TIMESTAMP
from sqlalchemy.orm import declarative_base
from sqlalchemy.dialects.postgresql import UUID as PGUUID
import uuid
from datetime import datetime, timezone

Base = declarative_base()


class StyleKeyword(Base):
    __tablename__ = "style_keywords"
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 归属（迁移 012）：NULL=admin 公共库，非空=个人库；唯一性为「同一 owner 内
    # style_name 唯一」（两个部分唯一索引，见迁移 012）
    owner_id = Column(PGUUID(as_uuid=True))
    style_name = Column(Text, nullable=False)
    keywords = Column(Text, nullable=False, default="")
    description = Column(Text, nullable=False, default="")
    # 变体轴（迁移 016，反同质化）：变体句池（换行/中文分号分隔），选定风格时
    # 按任务采样一条追加进描述词；空则回退 style_pick 内置变体池
    variants = Column(Text)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
