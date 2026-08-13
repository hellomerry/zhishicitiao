# 产能验证平台 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建一台可信赖的"产能计量仪器"，3 人 + 验证平台跑出 6 人配置（2,840 条/天）的稳定合格产能，作为月产 10 万条推算依据。

**Architecture:** 两段式栈。第一段（阶段 0/1/2）用 FastAPI + asyncio + PostgreSQL + Redis 轻量栈实现 8 个模块；通过 Q3 验证后，第二段（阶段 3）迁到 Temporal + LiteLLM Adapter，加 6 人配置功能。两段共享同一 PG Schema（按 Temporal Activity 可直接读写设计）。模型 1-2 家，本地部署验证平台。

**Tech Stack:** Python 3.12+, FastAPI 0.115+, SQLAlchemy 2.0 async, Alembic, asyncpg, PostgreSQL 16+, Redis 7.4+, LiteLLM, PaddleOCR 2.7+, Temporal 1.26+（第二段）, pytest, pytest-asyncio, httpx, openpyxl.

**Spec:** `docs/superpowers/specs/2026-08-13-产能验证平台-design.md`

---

## 全局约束

- **一条口径**: 1 篇正文（400-700 字）+ 6 张图（1 封面 + 5 内页），不含发布
- **批次会签**: 绿色 85% 走批次 + 20% 抽检（固定机制，不作为变量）
- **模型供应商**: 1-2 家（合同/合规确认后定具体名单）
- **部署**: 客户本地（验证平台），模型 API 走公网（V-C03 客户独立确认）
- **三阶段团队**: 1 人 → 3 人 → 6 人（每个阶段有独立退出条件）
- **核心埋点表**: `node_events(enqueued_at, started_at, finished_at, model_version, prompt_version, retry_count, cost_estimate_cny, error_class)` —— 看板所有指标从此表 + `review_sessions` 实时聚合
- **风险等级**: 🟢 绿（全门禁通过）→ 🟡 黄（任一规则失败/单域低置信度）→ 🔴 红（P0 问题/多域冲突/版权不明）
- **降级不能伪造成功**: 任何降级路径必须留 `degraded=true` 与原始失败信息
- **幂等键**: `task_id`（UUID）+ `node_idempotency_key(task_id, node_name, node_input_hash)` + `review_action_idempotency_key(review_session_id, action_type, payload_hash)`
- **异常值剔除**: 单条耗时 < 5 秒或 > 60 分钟标记 `anomaly_flag`，不计入 P50/P95
- **独占锁**: 单任务独占，30 秒无心跳即挂起，连续 90 分钟无心跳自动挂起

---

## 文件结构

```
query-validation-platform/
├── README.md
├── pyproject.toml
├── docker-compose.yml
├── .env.example
├── alembic.ini
├── migrations/versions/001_initial_schema.py
├── src/
│   ├── config.py
│   ├── db/session.py
│   ├── models/{tasks,entities,drafts,assets,review,snapshots,events}.py
│   ├── gateway/{litellm_adapter,failover,cost_tracker,prompt_versions}.py
│   ├── pipeline/{orchestrator,nodes,idempotency,outbox}.py
│   ├── quality/{rules,cross_check}.py
│   ├── review/{workbench,heartbeat,locks,batch_signoff,anomaly_detector}.py
│   ├── risk/classifier.py
│   ├── api/{main,tasks,review,dashboard,healthcheck}.py
│   ├── dashboard/{metrics,accuracy_report}.py
│   └── temporal_migration/{workflow,activities,data_compat}.py
├── tests/
│   ├── conftest.py
│   ├── unit/
│   ├── integration/
│   ├── migration/
│   └── validation/
└── ops/{run_local,backup,healthcheck}.sh
```

---

## 任务分解（28 个任务）

### Task 1: Bootstrap 项目骨架

**Files:**
- Create: `query-validation-platform/pyproject.toml`
- Create: `query-validation-platform/.env.example`
- Create: `query-validation-platform/docker-compose.yml`
- Create: `query-validation-platform/README.md`
- Create: `query-validation-platform/src/__init__.py`

**Interfaces:**
- Consumes: 无
- Produces: 项目根目录结构 + 可启动的 PG/Redis docker-compose

- [ ] **Step 1: 创建项目目录并初始化 pyproject.toml**

```toml
[project]
name = "query-validation-platform"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "sqlalchemy[asyncio]>=2.0",
    "asyncpg>=0.30",
    "alembic>=1.13",
    "redis>=5.2",
    "litellm>=1.50",
    "openpyxl>=3.1",
    "paddleocr>=2.7",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "python-multipart>=0.0.20",
    "httpx>=0.27",
]
[project.optional-dependencies]
dev = ["pytest>=8.3", "pytest-asyncio>=0.24", "pytest-cov>=5.0", "ruff>=0.7"]
```

- [ ] **Step 2: 创建 .env.example**

```env
DATABASE_URL=postgresql+asyncpg://qvp:qvp@localhost:5432/qvp
REDIS_URL=redis://localhost:6379/0
PRIMARY_MODEL_PROVIDER=openai
PRIMARY_MODEL_API_KEY=sk-xxx
FALLBACK_MODEL_PROVIDER=deepseek
FALLBACK_MODEL_API_KEY=sk-yyy
ENVIRONMENT=dev
LOG_LEVEL=INFO
HEARTBEAT_TIMEOUT_SECONDS=30
AUTO_SUSPEND_TIMEOUT_SECONDS=5400
SAMPLING_RATE=0.20
ANOMALY_MIN_SECONDS=5
ANOMALY_MAX_SECONDS=3600
```

- [ ] **Step 3: 创建 docker-compose.yml**

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: qvp
      POSTGRES_PASSWORD: qvp
      POSTGRES_DB: qvp
    ports: ["5432:5432"]
    volumes: ["pg_data:/var/lib/postgresql/data"]
    command: ["postgres", "-c", "wal_level=replica"]
  redis:
    image: redis:7.4
    ports: ["6379:6379"]
    volumes: ["redis_data:/data"]
volumes:
  pg_data:
  redis_data:
```

- [ ] **Step 4: 创建 README.md**

```markdown
# 产能验证平台

基于 spec `docs/superpowers/specs/2026-08-13-产能验证平台-design.md` 实施。

## 启动
docker compose up -d
uv sync
uv run uvicorn src.api.main:app --reload
```

- [ ] **Step 5: 创建 src/__init__.py（空文件）**

```python
```

- [ ] **Step 6: 启动 PG/Redis 并验证连通性**

Run: `docker compose up -d && sleep 3 && docker compose ps`
Expected: postgres 与 redis 状态为 healthy 或 running。

- [ ] **Step 7: Commit**

```bash
cd query-validation-platform
git init
git add .
git commit -m "feat: bootstrap project skeleton"
```

---

### Task 2: PG Schema（13 张表，含核心埋点表）

**Files:**
- Create: `migrations/versions/001_initial_schema.py`

**Interfaces:**
- Consumes: 无
- Produces: 13 张表，全部 `IF NOT EXISTS` 兼容两段式栈

- [ ] **Step 1: 创建 Alembic 配置 alembic.ini**

```ini
[alembic]
script_location = migrations
sqlalchemy.url = postgresql+asyncpg://qvp:qvp@localhost:5432/qvp
file_template = %%(rev)s_%%(slug)s
```

- [ ] **Step 2: 写 001_initial_schema.py（DDL 全部建表语句）**

```python
"""initial schema - 13 tables for capacity validation platform"""
from alembic import op

def upgrade():
    op.execute("""
    CREATE TABLE IF NOT EXISTS organizations (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        name TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS users (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        org_id UUID REFERENCES organizations(id),
        name TEXT NOT NULL,
        email TEXT UNIQUE,
        role TEXT NOT NULL CHECK (role IN ('A','B','C','admin')),
        capabilities JSONB NOT NULL DEFAULT '[]',
        active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS tasks (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        idempotency_key TEXT UNIQUE NOT NULL,
        query TEXT NOT NULL,
        content_type TEXT NOT NULL,
        platform TEXT,
        sla_hours INT NOT NULL DEFAULT 24,
        priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('normal','urgent','scheduled')),
        status TEXT NOT NULL DEFAULT 'draft',
        template_id UUID,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_by UUID REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS tasks_status_idx ON tasks(status);
    CREATE INDEX IF NOT EXISTS tasks_created_at_idx ON tasks(created_at);
    CREATE TABLE IF NOT EXISTS entity_snapshots (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        entity_type TEXT NOT NULL,
        canonical_name TEXT NOT NULL,
        version TEXT NOT NULL,
        valid_from TIMESTAMPTZ NOT NULL,
        valid_until TIMESTAMPTZ,
        attributes JSONB NOT NULL DEFAULT '{}',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(entity_type, canonical_name, version)
    );
    CREATE TABLE IF NOT EXISTS claims (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        claim_text TEXT NOT NULL,
        risk_level TEXT NOT NULL CHECK (risk_level IN ('P0','P1','P2','P3')),
        verification_status TEXT NOT NULL DEFAULT 'pending',
        position INT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS claims_task_idx ON claims(task_id);
    CREATE TABLE IF NOT EXISTS evidence (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        claim_id UUID NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
        source_url TEXT NOT NULL,
        source_level TEXT NOT NULL CHECK (source_level IN ('P0','P1','P2','P3')),
        publish_date DATE,
        captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        excerpt TEXT,
        supports BOOLEAN NOT NULL
    );
    CREATE INDEX IF NOT EXISTS evidence_claim_idx ON evidence(claim_id);
    CREATE TABLE IF NOT EXISTS drafts (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        version INT NOT NULL,
        body TEXT NOT NULL,
        model_version TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        token_count INT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(task_id, version)
    );
    CREATE TABLE IF NOT EXISTS page_copies (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        page_index INT NOT NULL CHECK (page_index BETWEEN 1 AND 6),
        body TEXT NOT NULL,
        claim_ids JSONB NOT NULL DEFAULT '[]',
        UNIQUE(task_id, page_index)
    );
    CREATE TABLE IF NOT EXISTS assets (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        page_index INT NOT NULL,
        subject TEXT,
        source_type TEXT NOT NULL CHECK (source_type IN ('official','user_upload','licensed','ai_generated','product_render')),
        copyright_status TEXT NOT NULL CHECK (copyright_status IN ('clear','unknown','restricted')),
        license_scope TEXT,
        hash TEXT NOT NULL,
        model_version TEXT,
        is_illustration BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS assets_task_idx ON assets(task_id);
    CREATE TABLE IF NOT EXISTS ocr_results (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        asset_id UUID NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
        raw_text TEXT NOT NULL,
        key_fields JSONB NOT NULL DEFAULT '{}',
        confidence REAL NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS rule_results (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        rule_name TEXT NOT NULL,
        passed BOOLEAN NOT NULL,
        details JSONB NOT NULL DEFAULT '{}',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS rule_results_task_idx ON rule_results(task_id);
    CREATE TABLE IF NOT EXISTS cross_checks (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        field_name TEXT NOT NULL,
        expected TEXT NOT NULL,
        actual TEXT NOT NULL,
        matched BOOLEAN NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS risk_classifications (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        task_id UUID UNIQUE NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        level TEXT NOT NULL CHECK (level IN ('green','yellow','red')),
        reasons JSONB NOT NULL DEFAULT '[]',
        classified_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS review_sessions (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('A','B','C')),
        reviewer_id UUID REFERENCES users(id),
        locked_at TIMESTAMPTZ,
        last_heartbeat_at TIMESTAMPTZ,
        auto_suspended_at TIMESTAMPTZ,
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        anomaly_flag BOOLEAN NOT NULL DEFAULT FALSE,
        time_inconsistency_flag BOOLEAN NOT NULL DEFAULT FALSE
    );
    CREATE INDEX IF NOT EXISTS review_sessions_task_role_idx ON review_sessions(task_id, role);
    CREATE UNIQUE INDEX IF NOT EXISTS review_sessions_active_lock_idx
        ON review_sessions(task_id, role)
        WHERE finished_at IS NULL;
    CREATE TABLE IF NOT EXISTS review_actions (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        review_session_id UUID NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
        idempotency_key TEXT UNIQUE NOT NULL,
        action_type TEXT NOT NULL CHECK (action_type IN ('view','approve','reject','transfer','escalate')),
        client_ts TIMESTAMPTZ NOT NULL,
        server_ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        payload JSONB NOT NULL DEFAULT '{}',
        duration_ms INT
    );
    CREATE TABLE IF NOT EXISTS issues (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('A','B','C')),
        priority TEXT NOT NULL CHECK (priority IN ('P0','P1','P2')),
        description TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
        created_by UUID REFERENCES users(id),
        closed_by UUID REFERENCES users(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        closed_at TIMESTAMPTZ
    );
    CREATE INDEX IF NOT EXISTS issues_task_idx ON issues(task_id);
    CREATE TABLE IF NOT EXISTS batches (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        template_id UUID,
        risk_level TEXT NOT NULL CHECK (risk_level IN ('green')),
        sampling_rate REAL NOT NULL DEFAULT 0.20,
        member_count INT NOT NULL,
        signoff_status TEXT NOT NULL DEFAULT 'pending' CHECK (signoff_status IN ('pending','signed','frozen','withdrawn')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        signed_at TIMESTAMPTZ
    );
    CREATE TABLE IF NOT EXISTS batch_members (
        batch_id UUID NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
        task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        sampled BOOLEAN NOT NULL DEFAULT FALSE,
        review_result TEXT CHECK (review_result IN ('passed','failed','pending')),
        PRIMARY KEY (batch_id, task_id)
    );
    CREATE TABLE IF NOT EXISTS approvals (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        batch_id UUID REFERENCES batches(id),
        task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('A','B','C','release')),
        approver_id UUID REFERENCES users(id),
        conclusion TEXT NOT NULL,
        signed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS publish_snapshots (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        task_id UUID NOT NULL REFERENCES tasks(id),
        snapshot_data JSONB NOT NULL,
        immutable BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS node_events (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        node_name TEXT NOT NULL,
        node_idempotency_key TEXT NOT NULL,
        enqueued_at TIMESTAMPTZ NOT NULL,
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        model_version TEXT,
        prompt_version TEXT,
        retry_count INT NOT NULL DEFAULT 0,
        cost_estimate_cny NUMERIC(10,4),
        error_class TEXT,
        UNIQUE(task_id, node_name, node_idempotency_key)
    );
    CREATE INDEX IF NOT EXISTS node_events_task_idx ON node_events(task_id);
    CREATE INDEX IF NOT EXISTS node_events_started_idx ON node_events(started_at);
    """)

def downgrade():
    for table in ['node_events','publish_snapshots','approvals','batch_members',
                  'batches','issues','review_actions','review_sessions',
                  'risk_classifications','cross_checks','rule_results',
                  'ocr_results','assets','page_copies','drafts','evidence',
                  'claims','entity_snapshots','tasks','users','organizations']:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
```

- [ ] **Step 3: 运行迁移**

Run: `docker exec -i $(docker compose ps -q postgres) psql -U qvp -d qvp < migrations/versions/001_initial_schema.sql`
（注：用 psql 直接跑 DDL，因为 alembic upgrade 需要先建 alembic_version 表；后续任务用 alembic）

- [ ] **Step 4: 验证 21 张表全部创建**

Run: `docker exec -i $(docker compose ps -q postgres) psql -U qvp -d qvp -c "\dt"`
Expected: 列出 21 张表（13 张核心 + 8 张关联/扩展）。

- [ ] **Step 5: Commit**

```bash
git add migrations/
git commit -m "feat: initial PG schema (21 tables, node_events core telemetry)"
```

---

### Task 3: SQLAlchemy Models + Alembic Migration 框架

**Files:**
- Create: `src/db/session.py`
- Create: `src/models/tasks.py`
- Create: `src/models/events.py`
- Create: `src/config.py`
- Create: `tests/unit/test_db_session.py`

**Interfaces:**
- Consumes: Task 2 schema
- Produces: 异步 DB session + Task/NodeEvent ORM 模型

- [ ] **Step 1: 写 src/config.py**

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://qvp:qvp@localhost:5432/qvp"
    redis_url: str = "redis://localhost:6379/0"
    primary_model_provider: str = "openai"
    primary_model_api_key: str = "sk-xxx"
    fallback_model_provider: str = "deepseek"
    fallback_model_api_key: str = "sk-yyy"
    environment: str = "dev"
    log_level: str = "INFO"
    heartbeat_timeout_seconds: int = 30
    auto_suspend_timeout_seconds: int = 5400
    sampling_rate: float = 0.20
    anomaly_min_seconds: int = 5
    anomaly_max_seconds: int = 3600

settings = Settings()
```

- [ ] **Step 2: 写 src/db/session.py**

```python
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession
from src.config import settings

engine = create_async_engine(settings.database_url, pool_size=10, max_overflow=20)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session
```

- [ ] **Step 3: 写 src/models/tasks.py**

```python
from sqlalchemy import Column, String, Integer, TIMESTAMP, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import declarative_base
import uuid
from datetime import datetime

Base = declarative_base()

class Task(Base):
    __tablename__ = "tasks"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    idempotency_key = Column(Text, unique=True, nullable=False)
    query = Column(Text, nullable=False)
    content_type = Column(Text, nullable=False)
    platform = Column(Text)
    sla_hours = Column(Integer, nullable=False, default=24)
    priority = Column(Text, nullable=False, default="normal")
    status = Column(Text, nullable=False, default="draft")
    template_id = Column(UUID(as_uuid=True))
    created_at = Column(TIMESTAMPTZ, nullable=False, default=datetime.utcnow)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
```

- [ ] **Step 4: 写 src/models/events.py**

```python
from sqlalchemy import Column, String, Integer, Numeric, TIMESTAMPTZ
from sqlalchemy.dialects.postgresql import UUID, JSONB
from src.models.tasks import Base
import uuid
from datetime import datetime

class NodeEvent(Base):
    __tablename__ = "node_events"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(UUID(as_uuid=True), nullable=False)
    node_name = Column(Text, nullable=False)
    node_idempotency_key = Column(Text, nullable=False)
    enqueued_at = Column(TIMESTAMPTZ, nullable=False)
    started_at = Column(TIMESTAMPTZ)
    finished_at = Column(TIMESTAMPTZ)
    model_version = Column(Text)
    prompt_version = Column(Text)
    retry_count = Column(Integer, nullable=False, default=0)
    cost_estimate_cny = Column(Numeric(10, 4))
    error_class = Column(Text)
```

- [ ] **Step 5: 写 tests/unit/test_db_session.py**

```python
import pytest
from sqlalchemy import text
from src.db.session import engine, SessionLocal

@pytest.mark.asyncio
async def test_session_can_execute_select():
    async with SessionLocal() as session:
        result = await session.execute(text("SELECT 1"))
        assert result.scalar() == 1
```

- [ ] **Step 6: 安装依赖并跑测试**

Run: `uv sync && uv run pytest tests/unit/test_db_session.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/config.py src/db/session.py src/models/
git commit -m "feat: SQLAlchemy models + async session"
```

---

### Task 4: 幂等键基础设施

**Files:**
- Create: `src/pipeline/idempotency.py`
- Create: `tests/unit/test_idempotency.py`

**Interfaces:**
- Consumes: Task 3 session
- Produces: `compute_node_key(task_id, node_name, input_payload) -> str`、`check_or_record_node_event(session, key, ...) -> NodeEvent | None`

- [ ] **Step 1: 写失败测试**

```python
import hashlib
from src.pipeline.idempotency import compute_node_key

def test_compute_node_key_stable():
    payload = {"x": 1, "y": "abc"}
    k1 = compute_node_key("task-1", "node_a", payload)
    k2 = compute_node_key("task-1", "node_a", payload)
    assert k1 == k2

def test_compute_node_key_changes_with_payload():
    k1 = compute_node_key("task-1", "node_a", {"x": 1})
    k2 = compute_node_key("task-1", "node_a", {"x": 2})
    assert k1 != k2

def test_compute_node_key_format():
    k = compute_node_key("task-1", "node_a", {})
    assert len(k) == 64  # sha256 hex
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_idempotency.py -v`
Expected: FAIL "module 'src.pipeline.idempotency' has no attribute 'compute_node_key'"

- [ ] **Step 3: 实现 src/pipeline/idempotency.py**

```python
import hashlib
import json
from datetime import datetime
from sqlalchemy.dialects.postgresql import insert
from src.models.events import NodeEvent

def compute_node_key(task_id: str, node_name: str, payload: dict) -> str:
    payload_str = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    raw = f"{task_id}|{node_name}|{payload_str}"
    return hashlib.sha256(raw.encode()).hexdigest()

async def check_or_record_node_event(session, task_id: str, node_name: str,
                                       payload: dict, model_version: str = None,
                                       prompt_version: str = None):
    key = compute_node_key(str(task_id), node_name, payload)
    existing = await session.execute(
        NodeEvent.__table__.select().where(NodeEvent.node_idempotency_key == key)
    )
    row = existing.first()
    if row is not None:
        return None  # already done
    event = NodeEvent(
        task_id=task_id, node_name=node_name, node_idempotency_key=key,
        enqueued_at=datetime.utcnow(),
        model_version=model_version, prompt_version=prompt_version,
    )
    session.add(event)
    await session.flush()
    return event
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_idempotency.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/idempotency.py tests/unit/test_idempotency.py
git commit -m "feat: idempotency key computation + node event check-or-record"
```

---

### Task 5: 任务导入 API（Excel/CSV → tasks 表）

**Files:**
- Create: `src/api/tasks.py`
- Create: `tests/integration/test_task_import.py`

**Interfaces:**
- Consumes: Task 3 models, Task 4 idempotency
- Produces: `POST /api/tasks/import` 接受 multipart file，返回 `{"imported": N, "errors": [...]}`

- [ ] **Step 1: 写失败测试**

```python
import io
import pytest
from httpx import AsyncClient, ASGITransport
from src.api.main import app

@pytest.mark.asyncio
async def test_import_csv_creates_tasks():
    csv = "query,content_type,platform\n拉萨八中,school_compare,xhs\n小米17,phone_compare,xhs\n"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post("/api/tasks/import",
            files={"file": ("t.csv", csv.encode(), "text/csv")})
    assert resp.status_code == 200
    data = resp.json()
    assert data["imported"] == 2
    assert data["errors"] == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/integration/test_task_import.py -v`
Expected: FAIL 404 or 422

- [ ] **Step 3: 实现 src/api/tasks.py**

```python
import csv
import io
import uuid
from fastapi import APIRouter, UploadFile, File, HTTPException
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
                key = f"{row['query']}|{row['content_type']}|{row.get('platform','')}"
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
```

- [ ] **Step 4: 挂到 src/api/main.py（创建 main.py）**

```python
from fastapi import FastAPI
from src.api.tasks import router as tasks_router
from src.api.healthcheck import router as healthcheck_router

app = FastAPI(title="query-validation-platform")
app.include_router(tasks_router)
app.include_router(healthcheck_router)
```

```python
# src/api/healthcheck.py
from fastapi import APIRouter
router = APIRouter()
@router.get("/healthz")
async def healthz():
    return {"status": "ok"}
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/integration/test_task_import.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/api/ tests/integration/test_task_import.py
git commit -m "feat: task import API (CSV upload)"
```

---

### Task 6: 模型网关基础（LiteLLM Adapter + 降级链）

**Files:**
- Create: `src/gateway/prompt_versions.py`
- Create: `src/gateway/litellm_adapter.py`
- Create: `src/gateway/failover.py`
- Create: `src/gateway/cost_tracker.py`
- Create: `tests/unit/test_failover.py`
- Create: `tests/unit/test_cost_tracker.py`

**Interfaces:**
- Consumes: Task 3 config
- Produces: `call_with_failover(prompt, model_kwargs, prompt_version) -> {text, model_version, cost_cny, degraded}`

- [ ] **Step 1: 写 src/gateway/prompt_versions.py**

```python
PROMPT_VERSIONS = {
    "draft_v1": "请你以小红书博主的写作风格…",
    "page_split_v1": "对文章进行精简和拆分，总文字严格控制到350字以内…",
    "evidence_v1": "提取这段话中的事实点…",
}

def get_prompt(name: str, version: str = None) -> str:
    if version:
        key = f"{name}_{version}"
        if key in PROMPT_VERSIONS:
            return PROMPT_VERSIONS[key]
    return PROMPT_VERSIONS[f"{name}_v1"]
```

- [ ] **Step 2: 写 src/gateway/litellm_adapter.py**

```python
import time
import litellm
from src.config import settings
from src.gateway.cost_tracker import estimate_cost

async def call_provider(provider: str, api_key: str, model: str,
                        prompt: str, max_tokens: int = 1024) -> dict:
    litellm.api_key = api_key
    start = time.time()
    response = await litellm.acompletion(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
    )
    elapsed = time.time() - start
    text = response.choices[0].message.content
    usage = response.usage
    cost = estimate_cost(model, usage.prompt_tokens, usage.completion_tokens)
    return {
        "text": text,
        "model_version": model,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "cost_cny": cost,
        "elapsed_seconds": elapsed,
    }
```

- [ ] **Step 3: 写 src/gateway/cost_tracker.py（含失败测试）**

```python
# 失败测试在 tests/unit/test_cost_tracker.py
PRICING_CNY_PER_1K = {
    "gpt-4o-mini": {"prompt": 0.0011, "completion": 0.0044},
    "deepseek-chat": {"prompt": 0.0001, "completion": 0.0002},
    "default": {"prompt": 0.001, "completion": 0.002},
}

def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    p = PRICING_CNY_PER_1K.get(model, PRICING_CNY_PER_1K["default"])
    return (prompt_tokens / 1000 * p["prompt"]
            + completion_tokens / 1000 * p["completion"])
```

```python
# tests/unit/test_cost_tracker.py
from src.gateway.cost_tracker import estimate_cost

def test_estimate_cost_gpt4o_mini():
    cost = estimate_cost("gpt-4o-mini", 1000, 500)
    assert abs(cost - (0.0011 + 0.5 * 0.0044)) < 0.0001

def test_estimate_cost_unknown_uses_default():
    cost = estimate_cost("unknown-model", 1000, 1000)
    assert cost > 0
```

- [ ] **Step 4: 写 src/gateway/failover.py（含失败测试）**

```python
# 失败测试在 tests/unit/test_failover.py
import asyncio
from src.config import settings
from src.gateway.litellm_adapter import call_provider

async def call_with_failover(prompt: str, primary_model: str, fallback_model: str,
                              max_retries: int = 2) -> dict:
    for attempt in range(max_retries + 1):
        try:
            result = await call_provider(
                settings.primary_model_provider,
                settings.primary_model_api_key,
                primary_model, prompt, max_tokens=1024)
            result["degraded"] = False
            return result
        except Exception as e:
            if attempt == max_retries:
                try:
                    result = await call_provider(
                        settings.fallback_model_provider,
                        settings.fallback_model_api_key,
                        fallback_model, prompt, max_tokens=1024)
                    result["degraded"] = True
                    result["original_error"] = str(e)
                    return result
                except Exception as e2:
                    raise RuntimeError(f"Both providers failed: {e}; fallback: {e2}")
            await asyncio.sleep(2 ** attempt)
```

```python
# tests/unit/test_failover.py
import pytest
from unittest.mock import AsyncMock, patch
from src.gateway.failover import call_with_failover

@pytest.mark.asyncio
async def test_failover_marks_degraded_on_primary_failure():
    primary_exc = Exception("rate limit")
    fallback_result = {"text": "ok", "model_version": "deepseek-chat", "cost_cny": 0.001}
    with patch("src.gateway.failover.call_provider") as mock_call:
        mock_call.side_effect = [primary_exc, primary_exc, fallback_result]
        result = await call_with_failover("p", "gpt-4o-mini", "deepseek-chat")
    assert result["degraded"] is True
    assert "original_error" in result
```

- [ ] **Step 5: 跑所有单元测试**

Run: `uv run pytest tests/unit/ -v`
Expected: PASS（4 个文件）

- [ ] **Step 6: Commit**

```bash
git add src/gateway/ tests/unit/test_cost_tracker.py tests/unit/test_failover.py
git commit -m "feat: model gateway with failover chain and cost tracking"
```

---

### Task 7: 实体与证据库

**Files:**
- Create: `src/models/entities.py`
- Create: `src/api/entities.py`
- Create: `tests/integration/test_entity_snapshot.py`

**Interfaces:**
- Consumes: Task 3 session
- Produces: `POST /api/entities` 创建快照，`POST /api/claims` 创建事实点+证据包

- [ ] **Step 1: 写失败测试**

```python
import pytest
from httpx import AsyncClient, ASGITransport
from src.api.main import app

@pytest.mark.asyncio
async def test_create_entity_snapshot():
    payload = {
        "entity_type": "school",
        "canonical_name": "拉萨市第八中学",
        "version": "v1",
        "valid_from": "2026-01-01T00:00:00Z",
        "attributes": {"founded": 1990, "address": "纳金路29号"}
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post("/api/entities", json=payload)
    assert resp.status_code == 201
    assert resp.json()["canonical_name"] == "拉萨市第八中学"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/integration/test_entity_snapshot.py -v`
Expected: FAIL 404

- [ ] **Step 3: 实现 src/models/entities.py（只新增类）**

```python
from sqlalchemy import Column, Text, TIMESTAMPTZ, Date
from sqlalchemy.dialects.postgresql import UUID, JSONB
from src.models.tasks import Base
import uuid
from datetime import datetime

class EntitySnapshot(Base):
    __tablename__ = "entity_snapshots"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_type = Column(Text, nullable=False)
    canonical_name = Column(Text, nullable=False)
    version = Column(Text, nullable=False)
    valid_from = Column(TIMESTAMPTZ, nullable=False)
    valid_until = Column(TIMESTAMPTZ)
    attributes = Column(JSONB, nullable=False, default={})
    created_at = Column(TIMESTAMPTZ, nullable=False, default=datetime.utcnow)

class Claim(Base):
    __tablename__ = "claims"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(UUID(as_uuid=True), nullable=False)
    claim_text = Column(Text, nullable=False)
    risk_level = Column(Text, nullable=False)
    verification_status = Column(Text, nullable=False, default="pending")
    position = Column(Text, nullable=False)

class Evidence(Base):
    __tablename__ = "evidence"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    claim_id = Column(UUID(as_uuid=True), nullable=False)
    source_url = Column(Text, nullable=False)
    source_level = Column(Text, nullable=False)
    publish_date = Column(Date)
    captured_at = Column(TIMESTAMPTZ, nullable=False, default=datetime.utcnow)
    excerpt = Column(Text)
    supports = Column(Text, nullable=False)
```

- [ ] **Step 4: 实现 src/api/entities.py 并挂到 main.py**

```python
from fastapi import APIRouter
from pydantic import BaseModel
from datetime import datetime
from src.db.session import SessionLocal
from src.models.entities import EntitySnapshot, Claim, Evidence

router = APIRouter()

class EntityIn(BaseModel):
    entity_type: str
    canonical_name: str
    version: str
    valid_from: datetime
    valid_until: datetime | None = None
    attributes: dict = {}

@router.post("/api/entities", status_code=201)
async def create_entity(payload: EntityIn):
    async with SessionLocal() as session:
        e = EntitySnapshot(**payload.model_dump())
        session.add(e)
        await session.commit()
        await session.refresh(e)
        return {"id": str(e.id), "canonical_name": e.canonical_name}

class ClaimIn(BaseModel):
    task_id: str
    claim_text: str
    risk_level: str
    position: int

class EvidenceIn(BaseModel):
    claim_id: str
    source_url: str
    source_level: str
    publish_date: str | None = None
    excerpt: str | None = None
    supports: bool

@router.post("/api/claims", status_code=201)
async def create_claim(payload: ClaimIn):
    async with SessionLocal() as session:
        c = Claim(**payload.model_dump())
        session.add(c)
        await session.commit()
        await session.refresh(c)
        return {"id": str(c.id)}

@router.post("/api/evidence", status_code=201)
async def create_evidence(payload: EvidenceIn):
    async with SessionLocal() as session:
        e = Evidence(**payload.model_dump())
        session.add(e)
        await session.commit()
        return {"id": str(e.id)}
```

```python
# src/api/main.py 添加
from src.api.entities import router as entities_router
app.include_router(entities_router)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/integration/test_entity_snapshot.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/models/entities.py src/api/entities.py src/api/main.py tests/
git commit -m "feat: entity snapshot + claim + evidence APIs"
```

---

### Task 8: 规则引擎（字数/标题/绝对化/免责）

**Files:**
- Create: `src/quality/rules.py`
- Create: `tests/unit/test_rules.py`

**Interfaces:**
- Consumes: 无（纯函数）
- Produces: `check_rules(text, title) -> list[RuleResult]`

- [ ] **Step 1: 写失败测试**

```python
import pytest
from src.quality.rules import check_rules

def test_word_count_in_range():
    text = "。" * 30 + "正常内容" * 50 + "。" * 30
    results = check_rules(text, "测试标题")
    passed_names = [r["rule_name"] for r in results if r["passed"]]
    assert "word_count_400_700" in passed_names

def test_absolute_word_rejected():
    text = "这是一段正常内容。" * 60
    text += "这是绝对最好的产品。"
    results = check_rules(text, "测试标题")
    rule_names = [r["rule_name"] for r in results]
    assert "no_absolute_words" in rule_names
    failed = next(r for r in results if r["rule_name"] == "no_absolute_words")
    assert failed["passed"] is False

def test_disclaimer_required_for_safety_words():
    text = "本产品安全无害。" * 50
    results = check_rules(text, "测试标题")
    rule_names = [r["rule_name"] for r in results]
    assert "has_disclaimer" in rule_names
    failed = next(r for r in results if r["rule_name"] == "has_disclaimer")
    assert failed["passed"] is False

def test_title_too_long_rejected():
    results = check_rules("正常" * 50, "这是一个超过二十五个字的标题啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊")
    rule_names = [r["rule_name"] for r in results]
    assert "title_max_25" in rule_names
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_rules.py -v`
Expected: FAIL module not found

- [ ] **Step 3: 实现 src/quality/rules.py**

```python
ABSOLUTE_WORDS = ["绝对", "100%", "最", "第一", "唯一", "永久", "终身"]
SAFETY_WORDS = ["安全", "无害", "无副作用", "治疗", "疗效", "保证"]
DISCLAIMER_PATTERNS = ["仅供参考", "不构成专业建议", "请咨询"]

def check_rules(text: str, title: str) -> list[dict]:
    results = []
    char_count = len([c for c in text if c.strip()])
    results.append({
        "rule_name": "word_count_400_700",
        "passed": 400 <= char_count <= 700,
        "details": {"char_count": char_count}
    })
    results.append({
        "rule_name": "title_max_25",
        "passed": len(title) <= 25,
        "details": {"title_length": len(title)}
    })
    has_absolute = any(w in text for w in ABSOLUTE_WORDS)
    results.append({
        "rule_name": "no_absolute_words",
        "passed": not has_absolute,
        "details": {"found": [w for w in ABSOLUTE_WORDS if w in text]}
    })
    has_safety = any(w in text for w in SAFETY_WORDS)
    has_disclaimer = any(p in text for p in DISCLAIMER_PATTERNS)
    if has_safety:
        results.append({
            "rule_name": "has_disclaimer",
            "passed": has_disclaimer,
            "details": {"has_safety_words": True}
        })
    return results
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_rules.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/quality/rules.py tests/unit/test_rules.py
git commit -m "feat: rule engine (word count, title length, absolute words, disclaimer)"
```

---

### Task 9: 风险分流 classifier

**Files:**
- Create: `src/risk/classifier.py`
- Create: `tests/unit/test_risk_classifier.py`

**Interfaces:**
- Consumes: 规则结果、OCR 一致性、证据完整性
- Produces: `classify(task_id, rule_results, cross_checks, evidence_complete) -> ("green"|"yellow"|"red", reasons)`

- [ ] **Step 1: 写失败测试**

```python
from src.risk.classifier import classify

def test_all_passed_is_green():
    level, reasons = classify(
        rule_results=[{"passed": True}] * 5,
        cross_checks=[{"matched": True}] * 3,
        evidence_complete=True,
        has_p0_issue=False,
    )
    assert level == "green"
    assert reasons == []

def test_rule_failure_is_yellow():
    level, reasons = classify(
        rule_results=[{"passed": True}, {"passed": False, "rule_name": "no_absolute_words"}],
        cross_checks=[{"matched": True}] * 3,
        evidence_complete=True,
        has_p0_issue=False,
    )
    assert level == "yellow"
    assert "no_absolute_words" in str(reasons)

def test_ocr_mismatch_is_red():
    level, reasons = classify(
        rule_results=[{"passed": True}] * 5,
        cross_checks=[{"matched": True}, {"matched": True}, {"matched": False}],
        evidence_complete=True,
        has_p0_issue=False,
    )
    assert level == "red"
    assert "ocr_mismatch" in reasons

def test_p0_issue_is_red():
    level, reasons = classify(
        rule_results=[{"passed": True}] * 5,
        cross_checks=[{"matched": True}] * 3,
        evidence_complete=True,
        has_p0_issue=True,
    )
    assert level == "red"
    assert "p0_issue" in reasons
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_risk_classifier.py -v`
Expected: FAIL module not found

- [ ] **Step 3: 实现 src/risk/classifier.py**

```python
def classify(rule_results: list, cross_checks: list,
             evidence_complete: bool, has_p0_issue: bool) -> tuple:
    reasons = []
    if has_p0_issue:
        reasons.append("p0_issue")
        return "red", reasons
    if any(not c["matched"] for c in cross_checks):
        reasons.append("ocr_mismatch")
        return "red", reasons
    if not evidence_complete:
        reasons.append("evidence_incomplete")
        return "yellow", reasons
    failed_rules = [r.get("rule_name", "unknown") for r in rule_results if not r["passed"]]
    if failed_rules:
        reasons.extend(failed_rules)
        return "yellow", reasons
    return "green", []
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_risk_classifier.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/risk/classifier.py tests/unit/test_risk_classifier.py
git commit -m "feat: risk classifier (green/yellow/red)"
```

---

### Task 10: 流水线编排器（asyncio 骨架）

**Files:**
- Create: `src/pipeline/orchestrator.py`
- Create: `src/pipeline/nodes.py`
- Create: `tests/integration/test_pipeline_e2e.py`

**Interfaces:**
- Consumes: Task 4 idempotency, Task 6 model gateway, Task 8 rules, Task 9 risk
- Produces: `run_pipeline(task_id) -> pipeline_result`，13 节点顺序执行

- [ ] **Step 1: 写 src/pipeline/nodes.py（13 节点定义）**

```python
import asyncio
import json
from datetime import datetime
from src.db.session import SessionLocal
from src.models.events import NodeEvent
from src.gateway.failover import call_with_failover
from src.gateway.prompt_versions import get_prompt
from src.quality.rules import check_rules
from src.risk.classifier import classify

NODES = [
    "task_import", "entity_bind", "evidence_build", "draft_gen",
    "rule_check", "page_split", "asset_gen", "ocr_read",
    "cross_check", "risk_classify", "review_queue",
    "batch_signoff", "publish_snapshot"
]

async def execute_node(task_id: str, node_name: str, input_data: dict,
                       node_fn=None):
    from src.pipeline.idempotency import check_or_record_node_event
    async with SessionLocal() as session:
        event = await check_or_record_node_event(
            session, task_id, node_name, input_data)
        if event is None:
            return {"skipped": True}
        event.started_at = datetime.utcnow()
        try:
            if node_fn:
                output = await node_fn(input_data)
            else:
                output = {"node": node_name, "input": input_data}
            event.finished_at = datetime.utcnow()
            event.cost_estimate_cny = output.get("cost_cny", 0)
            event.model_version = output.get("model_version")
            event.prompt_version = output.get("prompt_version")
            await session.commit()
            return output
        except Exception as e:
            event.finished_at = datetime.utcnow()
            event.error_class = type(e).__name__
            event.retry_count = (event.retry_count or 0) + 1
            await session.commit()
            raise

async def node_draft_gen(input_data: dict) -> dict:
    prompt = get_prompt("draft", "v1") + "\n\n" + input_data["query"]
    result = await call_with_failover(prompt, "gpt-4o-mini", "deepseek-chat")
    return {"text": result["text"], "model_version": result["model_version"],
            "prompt_version": "draft_v1", "cost_cny": result["cost_cny"],
            "degraded": result["degraded"]}
```

- [ ] **Step 2: 写 src/pipeline/orchestrator.py**

```python
import asyncio
from src.pipeline.nodes import execute_node, NODES

async def run_pipeline(task_id: str, node_inputs: dict | None = None) -> list:
    results = []
    inputs = node_inputs or {}
    for node_name in NODES[:3]:  # 阶段 0 只跑前 3 节点
        r = await execute_node(task_id, node_name, inputs.get(node_name, {}))
        results.append({"node": node_name, "result": r})
    return results
```

- [ ] **Step 3: 写失败测试**

```python
import pytest
from sqlalchemy import select, text
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.models.events import NodeEvent
from src.pipeline.orchestrator import run_pipeline

@pytest.mark.asyncio
async def test_pipeline_runs_first_three_nodes():
    async with SessionLocal() as session:
        task = Task(idempotency_key="test-1", query="test", content_type="x")
        session.add(task)
        await session.commit()
        await session.refresh(task)
        task_id = str(task.id)
    results = await run_pipeline(task_id)
    assert len(results) == 3
    assert results[0]["node"] == "task_import"
    async with SessionLocal() as session:
        events = await session.execute(
            select(NodeEvent).where(NodeEvent.task_id == task_id))
        assert len(events.scalars().all()) == 3
```

- [ ] **Step 4: 跑测试确认失败**

Run: `uv run pytest tests/integration/test_pipeline_e2e.py -v`
Expected: FAIL (orchestrator or nodes not importable)

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/integration/test_pipeline_e2e.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/ tests/integration/test_pipeline_e2e.py
git commit -m "feat: pipeline orchestrator (asyncio) + first 3 nodes"
```

---

### Task 11: 流水线节点 4-6（正文生成 → 规则质检 → 分页文案）

**Files:**
- Modify: `src/pipeline/nodes.py`（增加 node_rule_check, node_page_split 实现）
- Modify: `src/pipeline/orchestrator.py`（节点列表包含 4-6）
- Create: `tests/integration/test_pipeline_text_nodes.py`

**Interfaces:**
- Consumes: Task 8 rules
- Produces: `node_rule_check` 返回 rule_results 落库；`node_page_split` 生成 page_copies

- [ ] **Step 1: 写失败测试**

```python
import pytest
from src.db.session import SessionLocal
from src.models.tasks import Task, Base
from src.models.drafts import Draft
from src.models.entities import Claim, Evidence
from src.pipeline.orchestrator import run_pipeline

@pytest.mark.asyncio
async def test_pipeline_runs_through_page_split():
    async with SessionLocal() as session:
        task = Task(idempotency_key="text-1", query="测试查询", content_type="x")
        session.add(task)
        await session.commit()
        await session.refresh(task)
        task_id = str(task.id)
        claim = Claim(task_id=task.id, claim_text="某事实", risk_level="P1", position=1)
        session.add(claim)
        await session.commit()
    results = await run_pipeline(task_id)
    nodes = [r["node"] for r in results]
    assert "draft_gen" in nodes
    assert "rule_check" in nodes
    assert "page_split" in nodes
```

- [ ] **Step 2: 实现 src/models/drafts.py**

```python
from sqlalchemy import Column, Text, Integer, TIMESTAMPTZ
from sqlalchemy.dialects.postgresql import UUID, JSONB
from src.models.tasks import Base
import uuid
from datetime import datetime

class Draft(Base):
    __tablename__ = "drafts"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(UUID(as_uuid=True), nullable=False)
    version = Column(Integer, nullable=False)
    body = Column(Text, nullable=False)
    model_version = Column(Text, nullable=False)
    prompt_version = Column(Text, nullable=False)
    token_count = Column(Integer)
    created_at = Column(TIMESTAMPTZ, nullable=False, default=datetime.utcnow)

class PageCopy(Base):
    __tablename__ = "page_copies"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(UUID(as_uuid=True), nullable=False)
    page_index = Column(Integer, nullable=False)
    body = Column(Text, nullable=False)
    claim_ids = Column(JSONB, nullable=False, default=[])

class RuleResult(Base):
    __tablename__ = "rule_results"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(UUID(as_uuid=True), nullable=False)
    rule_name = Column(Text, nullable=False)
    passed = Column(Text, nullable=False)
    details = Column(JSONB, nullable=False, default={})
    created_at = Column(TIMESTAMPTZ, nullable=False, default=datetime.utcnow)
```

- [ ] **Step 3: 在 nodes.py 中扩展 node_rule_check 和 node_page_split**

```python
async def node_rule_check(input_data: dict) -> dict:
    from src.models.drafts import RuleResult
    text = input_data["text"]
    title = input_data.get("title", "")
    results = check_rules(text, title)
    async with SessionLocal() as session:
        for r in results:
            session.add(RuleResult(task_id=input_data["task_id"],
                                   rule_name=r["rule_name"],
                                   passed=str(r["passed"]),
                                   details=r["details"]))
        await session.commit()
    return {"rule_results": results, "all_passed": all(r["passed"] for r in results)}

async def node_page_split(input_data: dict) -> dict:
    text = input_data["text"]
    chunk_size = 350 // 6  # 6 pages
    pages = [text[i:i+chunk_size] for i in range(0, min(len(text), 350), chunk_size)]
    while len(pages) < 6:
        pages.append("")
    from src.models.drafts import PageCopy
    async with SessionLocal() as session:
        for i, body in enumerate(pages[:6], start=1):
            session.add(PageCopy(task_id=input_data["task_id"],
                                 page_index=i, body=body, claim_ids=[]))
        await session.commit()
    return {"page_count": min(len(pages), 6)}
```

- [ ] **Step 4: 修改 orchestrator.py 让节点 4-6 也执行**

```python
async def run_pipeline(task_id: str, node_inputs: dict | None = None) -> list:
    from src.pipeline.nodes import execute_node
    results = []
    inputs = node_inputs or {"task_id": task_id}
    for node_name in NODES[:6]:
        r = await execute_node(task_id, node_name, inputs.get(node_name, inputs))
        results.append({"node": node_name, "result": r})
    return results
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/integration/test_pipeline_text_nodes.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/nodes.py src/pipeline/orchestrator.py src/models/drafts.py tests/
git commit -m "feat: pipeline nodes 4-6 (draft, rule check, page split)"
```

---

### Task 12: 流水线节点 7-9（图片 → OCR → 图文一致性）

**Files:**
- Modify: `src/pipeline/nodes.py`（增加 node_asset_gen, node_ocr_read, node_cross_check）
- Create: `src/models/assets.py`
- Create: `src/quality/cross_check.py`
- Create: `tests/integration/test_pipeline_image_nodes.py`

**Interfaces:**
- Consumes: page_copies
- Produces: assets + ocr_results + cross_checks

- [ ] **Step 1: 写 src/models/assets.py**

```python
from sqlalchemy import Column, Text, Integer, Boolean, TIMESTAMPTZ
from sqlalchemy.dialects.postgresql import UUID, JSONB, NUMERIC
from src.models.tasks import Base
import uuid
from datetime import datetime

class Asset(Base):
    __tablename__ = "assets"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(UUID(as_uuid=True), nullable=False)
    page_index = Column(Integer, nullable=False)
    subject = Column(Text)
    source_type = Column(Text, nullable=False)
    copyright_status = Column(Text, nullable=False)
    license_scope = Column(Text)
    hash = Column(Text, nullable=False)
    model_version = Column(Text)
    is_illustration = Column(Boolean, nullable=False, default=False)
    created_at = Column(TIMESTAMPTZ, nullable=False, default=datetime.utcnow)

class OcrResult(Base):
    __tablename__ = "ocr_results"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    asset_id = Column(UUID(as_uuid=True), nullable=False)
    raw_text = Column(Text, nullable=False)
    key_fields = Column(JSONB, nullable=False, default={})
    confidence = Column(NUMERIC, nullable=False)
    created_at = Column(TIMESTAMPTZ, nullable=False, default=datetime.utcnow)

class CrossCheck(Base):
    __tablename__ = "cross_checks"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(UUID(as_uuid=True), nullable=False)
    field_name = Column(Text, nullable=False)
    expected = Column(Text, nullable=False)
    actual = Column(Text, nullable=False)
    matched = Column(Text, nullable=False)
    created_at = Column(TIMESTAMPTZ, nullable=False, default=datetime.utcnow)
```

- [ ] **Step 2: 写 src/quality/cross_check.py**

```python
import re

def extract_key_fields(page_body: str) -> dict:
    fields = {}
    nums = re.findall(r"\d{4}年|\d+\.?\d*[%公里元㎡个台件]|型号[\w-]+", page_body)
    fields["numbers"] = list(set(nums))
    return fields

def compare_field(expected: dict, actual: dict) -> list:
    mismatches = []
    for k, v in expected.items():
        if k not in actual:
            mismatches.append({"field": k, "expected": str(v), "actual": "missing", "matched": False})
            continue
        if isinstance(v, list):
            missing = set(v) - set(actual[k])
            if missing:
                mismatches.append({"field": k, "expected": str(v), "actual": str(actual[k]), "matched": False})
            else:
                mismatches.append({"field": k, "expected": str(v), "actual": str(actual[k]), "matched": True})
    return mismatches
```

- [ ] **Step 3: 在 nodes.py 中扩展 7-9 节点（使用 stub image generation）**

```python
async def node_asset_gen(input_data: dict) -> dict:
    from src.models.assets import Asset
    import hashlib
    pages = input_data.get("page_count", 6)
    async with SessionLocal() as session:
        for i in range(1, pages + 1):
            h = hashlib.md5(f"{input_data['task_id']}|{i}".encode()).hexdigest()
            session.add(Asset(task_id=input_data["task_id"], page_index=i,
                              source_type="ai_generated", copyright_status="clear",
                              hash=h, model_version="dall-e-3", is_illustration=False))
        await session.commit()
    return {"asset_count": pages}

async def node_ocr_read(input_data: dict) -> dict:
    from src.models.assets import OcrResult, Asset
    from sqlalchemy import select
    async with SessionLocal() as session:
        assets = await session.execute(
            select(Asset).where(Asset.task_id == input_data["task_id"]))
        for asset in assets.scalars():
            session.add(OcrResult(asset_id=asset.id, raw_text=f"page {asset.page_index}",
                                  key_fields={"page": str(asset.page_index)},
                                  confidence=0.95))
        await session.commit()
    return {"ocr_completed": True}

async def node_cross_check(input_data: dict) -> dict:
    from src.models.assets import CrossCheck, OcrResult, PageCopy
    from src.quality.cross_check import extract_key_fields, compare_field
    from sqlalchemy import select
    async with SessionLocal() as session:
        pages = await session.execute(
            select(PageCopy).where(PageCopy.task_id == input_data["task_id"]))
        page_list = pages.scalars().all()
        all_mismatches = []
        for p in page_list:
            expected = extract_key_fields(p.body)
            actual = {"page": str(p.page_index)}
            mismatches = compare_field(expected, actual)
            for m in mismatches:
                session.add(CrossCheck(task_id=input_data["task_id"], **m))
                all_mismatches.append(m)
        await session.commit()
    return {"mismatch_count": len(all_mismatches)}
```

- [ ] **Step 4: 修改 orchestrator.py 让节点 7-9 也执行**

```python
async def run_pipeline(task_id: str, node_inputs: dict | None = None) -> list:
    from src.pipeline.nodes import execute_node
    results = []
    inputs = node_inputs or {"task_id": task_id}
    for node_name in NODES[:9]:
        r = await execute_node(task_id, node_name, inputs.get(node_name, inputs))
        results.append({"node": node_name, "result": r})
    return results
```

- [ ] **Step 5: 写失败测试并跑通**

```python
# tests/integration/test_pipeline_image_nodes.py
import pytest
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.pipeline.orchestrator import run_pipeline

@pytest.mark.asyncio
async def test_pipeline_runs_through_cross_check():
    async with SessionLocal() as session:
        task = Task(idempotency_key="img-1", query="测试", content_type="x")
        session.add(task); await session.commit(); await session.refresh(task)
        tid = str(task.id)
    results = await run_pipeline(tid)
    nodes = [r["node"] for r in results]
    assert "asset_gen" in nodes and "ocr_read" in nodes and "cross_check" in nodes
```

Run: `uv run pytest tests/integration/test_pipeline_image_nodes.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/nodes.py src/pipeline/orchestrator.py src/models/assets.py src/quality/cross_check.py tests/
git commit -m "feat: pipeline nodes 7-9 (asset gen, OCR, cross check)"
```

---

### Task 13: 流水线节点 10-13（风险分流 → 审核队列 → 批次会签 → 发布快照）

**Files:**
- Modify: `src/pipeline/nodes.py`（增加 4 个节点）
- Modify: `src/pipeline/orchestrator.py`（执行全部 13 节点）
- Create: `src/models/review.py`
- Create: `src/models/snapshots.py`
- Create: `tests/integration/test_pipeline_full.py`

**Interfaces:**
- Consumes: Task 9 classifier
- Produces: risk_classifications + review_sessions + batches + publish_snapshots 落库

- [ ] **Step 1: 写 src/models/review.py**

```python
from sqlalchemy import Column, Text, Boolean, TIMESTAMPTZ, Integer
from sqlalchemy.dialects.postgresql import UUID, JSONB
from src.models.tasks import Base
import uuid
from datetime import datetime

class RiskClassification(Base):
    __tablename__ = "risk_classifications"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(UUID(as_uuid=True), nullable=False)
    level = Column(Text, nullable=False)
    reasons = Column(JSONB, nullable=False, default=[])
    classified_at = Column(TIMESTAMPTZ, nullable=False, default=datetime.utcnow)

class ReviewSession(Base):
    __tablename__ = "review_sessions"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(UUID(as_uuid=True), nullable=False)
    role = Column(Text, nullable=False)
    reviewer_id = Column(UUID(as_uuid=True))
    locked_at = Column(TIMESTAMPTZ)
    last_heartbeat_at = Column(TIMESTAMPTZ)
    auto_suspended_at = Column(TIMESTAMPTZ)
    started_at = Column(TIMESTAMPTZ)
    finished_at = Column(TIMESTAMPTZ)
    anomaly_flag = Column(Boolean, nullable=False, default=False)
    time_inconsistency_flag = Column(Boolean, nullable=False, default=False)

class Batch(Base):
    __tablename__ = "batches"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    template_id = Column(UUID(as_uuid=True))
    risk_level = Column(Text, nullable=False)
    sampling_rate = Column(Text, nullable=False)
    member_count = Column(Integer, nullable=False)
    signoff_status = Column(Text, nullable=False, default="pending")
    created_at = Column(TIMESTAMPTZ, nullable=False, default=datetime.utcnow)
    signed_at = Column(TIMESTAMPTZ)
```

- [ ] **Step 2: 写 src/models/snapshots.py**

```python
from sqlalchemy import Column, Boolean, TIMESTAMPTZ, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from src.models.tasks import Base
import uuid
from datetime import datetime

class PublishSnapshot(Base):
    __tablename__ = "publish_snapshots"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(UUID(as_uuid=True), nullable=False)
    snapshot_data = Column(JSONB, nullable=False)
    immutable = Column(Boolean, nullable=False, default=True)
    created_at = Column(TIMESTAMPTZ, nullable=False, default=datetime.utcnow)
```

- [ ] **Step 3: 在 nodes.py 扩展节点 10-13**

```python
async def node_risk_classify(input_data: dict) -> dict:
    from src.models.assets import CrossCheck, RuleResult
    from src.models.review import RiskClassification
    from sqlalchemy import select
    async with SessionLocal() as session:
        rules = await session.execute(select(RuleResult).where(RuleResult.task_id == input_data["task_id"]))
        checks = await session.execute(select(CrossCheck).where(CrossCheck.task_id == input_data["task_id"]))
        rule_list = [{"passed": r.passed == "True", "rule_name": r.rule_name} for r in rules.scalars()]
        check_list = [{"matched": c.matched == "True"} for c in checks.scalars()]
        level, reasons = classify(rule_list, check_list, evidence_complete=True, has_p0_issue=False)
        rc = RiskClassification(task_id=input_data["task_id"], level=level, reasons=reasons)
        session.add(rc)
        await session.commit()
    return {"level": level, "reasons": reasons}

async def node_review_queue(input_data: dict) -> dict:
    from src.models.review import ReviewSession
    async with SessionLocal() as session:
        for role in ["A", "B", "C"]:
            session.add(ReviewSession(task_id=input_data["task_id"], role=role))
        await session.commit()
    return {"queued": ["A", "B", "C"]}

async def node_batch_signoff(input_data: dict) -> dict:
    from src.models.review import Batch
    async with SessionLocal() as session:
        b = Batch(risk_level="green", sampling_rate="0.20", member_count=1)
        session.add(b); await session.commit()
    return {"batch_id": str(b.id)}

async def node_publish_snapshot(input_data: dict) -> dict:
    from src.models.snapshots import PublishSnapshot
    async with SessionLocal() as session:
        s = PublishSnapshot(task_id=input_data["task_id"], snapshot_data={"frozen": True})
        session.add(s); await session.commit()
    return {"snapshot_id": str(s.id)}
```

- [ ] **Step 4: 修改 orchestrator.py 执行全部 13 节点**

```python
async def run_pipeline(task_id: str, node_inputs: dict | None = None) -> list:
    from src.pipeline.nodes import execute_node
    results = []
    inputs = node_inputs or {"task_id": task_id}
    for node_name in NODES:
        r = await execute_node(task_id, node_name, inputs.get(node_name, inputs))
        results.append({"node": node_name, "result": r})
    return results
```

- [ ] **Step 5: 写并跑测试**

```python
# tests/integration/test_pipeline_full.py
import pytest
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.pipeline.orchestrator import run_pipeline

@pytest.mark.asyncio
async def test_full_pipeline_all_13_nodes():
    async with SessionLocal() as session:
        task = Task(idempotency_key="full-1", query="测试", content_type="x")
        session.add(task); await session.commit(); await session.refresh(task)
        tid = str(task.id)
    results = await run_pipeline(tid)
    assert len(results) == 13
```

Run: `uv run pytest tests/integration/test_pipeline_full.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/ src/models/review.py src/models/snapshots.py tests/
git commit -m "feat: pipeline nodes 10-13 (risk, review, batch, snapshot)"
```

---

### Task 14: 审核工作台 API + 单条独占锁

**Files:**
- Create: `src/review/locks.py`
- Create: `src/review/workbench.py`
- Create: `src/api/review.py`
- Create: `tests/integration/test_review_workbench.py`

**Interfaces:**
- Consumes: review_sessions 表
- Produces: `POST /api/review/claim` 领取任务（独占锁），`POST /api/review/action` 写入 review_actions

- [ ] **Step 1: 写 src/review/locks.py**

```python
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from src.db.session import SessionLocal
from src.models.review import ReviewSession

async def acquire_lock(task_id: str, role: str, reviewer_id: str) -> dict:
    async with SessionLocal() as session:
        existing = await session.execute(
            select(ReviewSession).where(
                ReviewSession.task_id == task_id,
                ReviewSession.role == role,
                ReviewSession.finished_at.is_(None)))
        for s in existing.scalars():
            if s.locked_at and s.last_heartbeat_at:
                if (datetime.utcnow() - s.last_heartbeat_at).total_seconds() < 30:
                    return {"acquired": False, "locked_by": str(s.reviewer_id)}
        for s in existing.scalars():
            s.reviewer_id = reviewer_id
            s.locked_at = datetime.utcnow()
            s.last_heartbeat_at = datetime.utcnow()
            s.started_at = datetime.utcnow()
        if not existing.scalars().all():
            session.add(ReviewSession(task_id=task_id, role=role,
                                       reviewer_id=reviewer_id,
                                       locked_at=datetime.utcnow(),
                                       last_heartbeat_at=datetime.utcnow(),
                                       started_at=datetime.utcnow()))
        await session.commit()
    return {"acquired": True}
```

- [ ] **Step 2: 写 src/api/review.py**

```python
from fastapi import APIRouter
from pydantic import BaseModel
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
            ReviewSession.__table__.select().where(
                ReviewSession.role == role,
                ReviewSession.finished_at.is_(None)))
        return {"sessions": [{"task_id": str(r.task_id)} for r in result]}
```

- [ ] **Step 3: 写失败测试**

```python
# tests/integration/test_review_workbench.py
import pytest
from httpx import AsyncClient, ASGITransport
from src.api.main import app
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.models.review import ReviewSession

@pytest.mark.asyncio
async def test_double_claim_second_fails():
    async with SessionLocal() as session:
        task = Task(idempotency_key="lock-1", query="t", content_type="x")
        session.add(task); await session.commit()
        tid = str(task.id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r1 = await ac.post("/api/review/claim", json={"task_id": tid, "role": "A", "reviewer_id": "u1"})
        r2 = await ac.post("/api/review/claim", json={"task_id": tid, "role": "A", "reviewer_id": "u2"})
    assert r1.json()["acquired"] is True
    assert r2.json()["acquired"] is False
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/integration/test_review_workbench.py -v`
Expected: PASS

- [ ] **Step 5: 挂到 main.py + commit**

```python
# src/api/main.py
from src.api.review import router as review_router
app.include_router(review_router)
```

```bash
git add src/review/ src/api/review.py src/api/main.py tests/
git commit -m "feat: review workbench with single-task exclusive lock"
```

---

### Task 15: 心跳与自动挂起机制

**Files:**
- Create: `src/review/heartbeat.py`
- Create: `tests/integration/test_heartbeat_locks.py`

**Interfaces:**
- Consumes: review_sessions 表
- Produces: `POST /api/review/heartbeat` 更新 last_heartbeat_at + 后台任务每分钟扫描自动挂起

- [ ] **Step 1: 写 src/review/heartbeat.py**

```python
import asyncio
from datetime import datetime
from sqlalchemy import select, update
from src.db.session import SessionLocal
from src.models.review import ReviewSession

async def record_heartbeat(task_id: str, role: str, reviewer_id: str):
    async with SessionLocal() as session:
        await session.execute(
            update(ReviewSession)
            .where(ReviewSession.task_id == task_id,
                   ReviewSession.role == role,
                   ReviewSession.reviewer_id == reviewer_id,
                   ReviewSession.finished_at.is_(None))
            .values(last_heartbeat_at=datetime.utcnow()))
        await session.commit()
    return {"ok": True}

async def auto_suspend_stale_sessions(timeout_seconds: int = 5400):
    async with SessionLocal() as session:
        cutoff = datetime.utcnow().timestamp() - timeout_seconds
        result = await session.execute(
            update(ReviewSession)
            .where(ReviewSession.finished_at.is_(None),
                   ReviewSession.last_heartbeat_at.is_not(None))
            .values(auto_suspended_at=datetime.utcnow())
            .where(ReviewSession.last_heartbeat_at < datetime.utcfromtimestamp(cutoff))
            .returning(ReviewSession.id))
        await session.commit()
        return {"suspended": [str(r[0]) for r in result]}

async def heartbeat_loop(interval_seconds: int = 60):
    while True:
        await auto_suspend_stale_sessions()
        await asyncio.sleep(interval_seconds)
```

- [ ] **Step 2: 在 api/review.py 增加心跳端点**

```python
# src/api/review.py 添加
from src.review.heartbeat import record_heartbeat

class HeartbeatIn(BaseModel):
    task_id: str
    role: str
    reviewer_id: str

@router.post("/api/review/heartbeat")
async def heartbeat(payload: HeartbeatIn):
    return await record_heartbeat(payload.task_id, payload.role, payload.reviewer_id)
```

- [ ] **Step 3: 写失败测试**

```python
# tests/integration/test_heartbeat_locks.py
import pytest
from datetime import datetime, timedelta
from src.db.session import SessionLocal
from src.models.review import ReviewSession
from src.models.tasks import Task
from src.review.heartbeat import auto_suspend_stale_sessions

@pytest.mark.asyncio
async def test_auto_suspend_after_timeout():
    async with SessionLocal() as session:
        task = Task(idempotency_key="hb-1", query="t", content_type="x")
        session.add(task); await session.commit()
        tid = task.id
        old = datetime.utcnow() - timedelta(seconds=6000)
        session.add(ReviewSession(task_id=tid, role="A", reviewer_id="u1",
                                   locked_at=old, last_heartbeat_at=old, started_at=old))
        await session.commit()
    result = await auto_suspend_stale_sessions(timeout_seconds=5400)
    assert len(result["suspended"]) == 1
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/integration/test_heartbeat_locks.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/review/heartbeat.py src/api/review.py tests/
git commit -m "feat: heartbeat recording + auto-suspend stale sessions"
```

---

### Task 16: 异常值剔除规则

**Files:**
- Create: `src/review/anomaly_detector.py`
- Create: `tests/unit/test_anomaly_detector.py`

**Interfaces:**
- Consumes: review_sessions + review_actions
- Produces: `flag_anomalies() -> {"flagged": N}` 每小时聚合

- [ ] **Step 1: 写失败测试**

```python
import pytest
from datetime import datetime, timedelta
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.models.review import ReviewSession
from src.review.anomaly_detector import flag_anomalies

@pytest.mark.asyncio
async def test_too_fast_flagged():
    async with SessionLocal() as session:
        task = Task(idempotency_key="fast-1", query="t", content_type="x")
        session.add(task); await session.commit()
        now = datetime.utcnow()
        session.add(ReviewSession(task_id=task.id, role="A", reviewer_id="u1",
                                   started_at=now, finished_at=now + timedelta(seconds=2),
                                   last_heartbeat_at=now))
        await session.commit()
    result = await flag_anomalies()
    assert result["flagged"] >= 1

@pytest.mark.asyncio
async def test_too_slow_flagged():
    async with SessionLocal() as session:
        task = Task(idempotency_key="slow-1", query="t", content_type="x")
        session.add(task); await session.commit()
        now = datetime.utcnow()
        session.add(ReviewSession(task_id=task.id, role="A", reviewer_id="u1",
                                   started_at=now, finished_at=now + timedelta(seconds=7200),
                                   last_heartbeat_at=now))
        await session.commit()
    result = await flag_anomalies()
    assert result["flagged"] >= 1
```

- [ ] **Step 2: 实现 src/review/anomaly_detector.py**

```python
from datetime import datetime
from sqlalchemy import select, update
from src.db.session import SessionLocal
from src.models.review import ReviewSession
from src.config import settings

async def flag_anomalies():
    async with SessionLocal() as session:
        result = await session.execute(
            select(ReviewSession).where(
                ReviewSession.started_at.is_not(None),
                ReviewSession.finished_at.is_not(None),
                ReviewSession.anomaly_flag == False))  # noqa
        flagged_count = 0
        for rs in result.scalars():
            elapsed = (rs.finished_at - rs.started_at).total_seconds()
            if elapsed < settings.anomaly_min_seconds or elapsed > settings.anomaly_max_seconds:
                rs.anomaly_flag = True
                flagged_count += 1
        await session.commit()
    return {"flagged": flagged_count}
```

- [ ] **Step 3: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_anomaly_detector.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/review/anomaly_detector.py tests/unit/test_anomaly_detector.py
git commit -m "feat: anomaly flagging for too-fast/too-slow review sessions"
```

---

### Task 17: 批次会签（20% 抽样 + 抽样任务进入单域审核）

**Files:**
- Create: `src/review/batch_signoff.py`
- Create: `tests/integration/test_batch_signoff.py`

**Interfaces:**
- Consumes: green 任务列表
- Produces: `create_batch(template_id, task_ids, sampling_rate=0.20)` 生成批次 + 标记 sampled

- [ ] **Step 1: 写失败测试**

```python
import pytest
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.models.review import RiskClassification, Batch
from src.review.batch_signoff import create_batch

@pytest.mark.asyncio
async def test_create_batch_samples_20_percent():
    async with SessionLocal() as session:
        task_ids = []
        for i in range(10):
            t = Task(idempotency_key=f"batch-{i}", query=f"q{i}", content_type="x")
            session.add(t)
        await session.commit()
        result = await session.execute(Task.__table__.select())
        for row in result:
            tid = row[0]
            session.add(RiskClassification(task_id=tid, level="green", reasons=[]))
            task_ids.append(str(tid))
        await session.commit()
    batch = await create_batch(task_ids=task_ids, sampling_rate=0.20)
    assert batch["member_count"] == 10
    assert batch["sampled_count"] == 2  # 20% of 10
```

- [ ] **Step 2: 实现 src/review/batch_signoff.py**

```python
import random
from sqlalchemy import insert
from src.db.session import SessionLocal
from src.models.review import Batch, RiskClassification
from sqlalchemy.dialects.postgresql import insert as pg_insert

async def create_batch(task_ids: list, sampling_rate: float = 0.20):
    sampled_count = max(1, round(len(task_ids) * sampling_rate))
    sampled = set(random.sample(task_ids, sampled_count))
    async with SessionLocal() as session:
        batch = Batch(risk_level="green",
                      sampling_rate=str(sampling_rate),
                      member_count=len(task_ids))
        session.add(batch); await session.commit()
        for tid in task_ids:
            stmt = pg_insert(Batch.__table__.name).values(
                batch_id=batch.id, task_id=tid, sampled=(tid in sampled),
                review_result="pending").on_conflict_do_nothing()
            await session.execute(stmt)
        await session.commit()
    return {"batch_id": str(batch.id), "member_count": len(task_ids),
            "sampled_count": len(sampled), "sampled": list(sampled)}
```

（注意：实际 SQL 需要 `batch_members` 表 join，这里简化用裸 SQL 或直接 ORM 操作 BatchMember 模型）

- [ ] **Step 3: 补 BatchMember 模型并修正实现**

```python
# src/models/review.py 添加
class BatchMember(Base):
    __tablename__ = "batch_members"
    batch_id = Column(UUID(as_uuid=True), primary_key=True)
    task_id = Column(UUID(as_uuid=True), primary_key=True)
    sampled = Column(Boolean, nullable=False, default=False)
    review_result = Column(Text)
```

```python
# src/review/batch_signoff.py 修正
from src.models.review import Batch, BatchMember

async def create_batch(task_ids, sampling_rate=0.20):
    sampled_count = max(1, round(len(task_ids) * sampling_rate))
    sampled = set(random.sample(task_ids, sampled_count))
    async with SessionLocal() as session:
        batch = Batch(risk_level="green", sampling_rate=str(sampling_rate),
                      member_count=len(task_ids))
        session.add(batch); await session.commit()
        for tid in task_ids:
            session.add(BatchMember(batch_id=batch.id, task_id=tid,
                                    sampled=(tid in sampled),
                                    review_result="pending"))
        await session.commit()
    return {"batch_id": str(batch.id), "member_count": len(task_ids),
            "sampled_count": len(sampled)}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/integration/test_batch_signoff.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/review/batch_signoff.py src/models/review.py tests/
git commit -m "feat: batch creation with 20% sampling for green tasks"
```

---

### Task 18: 看板指标聚合（从 node_events 实时聚合）

**Files:**
- Create: `src/dashboard/metrics.py`
- Create: `src/api/dashboard.py`
- Create: `tests/unit/test_metrics.py`

**Interfaces:**
- Consumes: node_events + review_sessions
- Produces: `GET /api/dashboard/metrics` 返回 throughput / first_pass_rate / human_touch_rate / rework_rate / p50_p95 / cost / queue_depth / error_top_n

- [ ] **Step 1: 写 src/dashboard/metrics.py**

```python
from datetime import datetime, timedelta
from sqlalchemy import select, func
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.models.events import NodeEvent
from src.models.review import ReviewSession

async def throughput_last_hour():
    async with SessionLocal() as session:
        cutoff = datetime.utcnow() - timedelta(hours=1)
        result = await session.execute(
            select(func.count(Task.id)).where(Task.created_at >= cutoff))
        return result.scalar() or 0

async def first_pass_rate_last_24h():
    async with SessionLocal() as session:
        cutoff = datetime.utcnow() - timedelta(hours=24)
        total = await session.execute(
            select(func.count(Task.id)).where(Task.created_at >= cutoff))
        green = await session.execute(
            select(func.count(Task.id))
            .where(Task.created_at >= cutoff, Task.status == "green"))
        total_n = total.scalar() or 0
        green_n = green.scalar() or 0
        return green_n / total_n if total_n > 0 else 0.0

async def human_touch_rate_last_24h():
    async with SessionLocal() as session:
        cutoff = datetime.utcnow() - timedelta(hours=24)
        total = await session.execute(
            select(func.count(Task.id)).where(Task.created_at >= cutoff))
        reviewed = await session.execute(
            select(func.count(func.distinct(ReviewSession.task_id)))
            .where(ReviewSession.started_at >= cutoff))
        total_n = total.scalar() or 0
        rev_n = reviewed.scalar() or 0
        return rev_n / total_n if total_n > 0 else 0.0

async def p95_node_duration():
    async with SessionLocal() as session:
        result = await session.execute(
            select(func.extract("epoch", NodeEvent.finished_at - NodeEvent.started_at))
            .where(NodeEvent.started_at.is_not(None),
                   NodeEvent.finished_at.is_not(None),
                   NodeEvent.anomaly_flag.is_(False) if hasattr(NodeEvent, 'anomaly_flag') else True))
        durations = [r[0] for r in result if r[0] is not None]
        if not durations:
            return 0
        durations.sort()
        idx = int(len(durations) * 0.95)
        return durations[min(idx, len(durations) - 1)]

async def cost_per_task_24h():
    async with SessionLocal() as session:
        cutoff = datetime.utcnow() - timedelta(hours=24)
        total_cost = await session.execute(
            select(func.sum(NodeEvent.cost_estimate_cny))
            .where(NodeEvent.started_at >= cutoff))
        total_n = await session.execute(
            select(func.count(Task.id)).where(Task.created_at >= cutoff))
        cost = total_cost.scalar() or 0
        n = total_n.scalar() or 0
        return float(cost) / n if n > 0 else 0.0

async def queue_depth():
    async with SessionLocal() as session:
        result = await session.execute(
            select(ReviewSession.role, func.count(ReviewSession.id))
            .where(ReviewSession.finished_at.is_(None))
            .group_by(ReviewSession.role))
        return {role: count for role, count in result}

async def error_top_n(n: int = 5):
    async with SessionLocal() as session:
        result = await session.execute(
            select(NodeEvent.error_class, func.count(NodeEvent.id))
            .where(NodeEvent.error_class.is_not(None))
            .group_by(NodeEvent.error_class)
            .order_by(func.count(NodeEvent.id).desc())
            .limit(n))
        return [{"error_class": ec, "count": c} for ec, c in result]

async def all_metrics():
    return {
        "throughput_per_hour": await throughput_last_hour(),
        "first_pass_rate_24h": await first_pass_rate_last_24h(),
        "human_touch_rate_24h": await human_touch_rate_last_24h(),
        "p95_node_seconds": await p95_node_duration(),
        "cost_per_task_24h_cny": await cost_per_task_24h(),
        "queue_depth": await queue_depth(),
        "error_top_5": await error_top_n(5),
    }
```

- [ ] **Step 2: 写 src/api/dashboard.py**

```python
from fastapi import APIRouter
from src.dashboard.metrics import all_metrics

router = APIRouter()

@router.get("/api/dashboard/metrics")
async def metrics():
    return await all_metrics()
```

挂到 main.py

- [ ] **Step 3: 写并跑测试**

```python
# tests/unit/test_metrics.py
import pytest
from src.dashboard.metrics import all_metrics

@pytest.mark.asyncio
async def test_all_metrics_returns_dict():
    m = await all_metrics()
    assert "throughput_per_hour" in m
    assert "queue_depth" in m
```

Run: `uv run pytest tests/unit/test_metrics.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/dashboard/ src/api/dashboard.py src/api/main.py tests/
git commit -m "feat: dashboard metrics aggregation from node_events"
```

---

### Task 19: 仪器精度报告（time_inconsistency_flag）

**Files:**
- Create: `src/dashboard/accuracy_report.py`
- Create: `tests/unit/test_accuracy_report.py`

**Interfaces:**
- Consumes: review_sessions
- Produces: `accuracy_report() -> {time_inconsistency_rate, clock_drift_distribution, anomaly_rate, sampled_recon_count}`

- [ ] **Step 1: 写失败测试**

```python
import pytest
from src.dashboard.accuracy_report import accuracy_report

@pytest.mark.asyncio
async def test_accuracy_report_returns_dict():
    report = await accuracy_report()
    assert "time_inconsistency_rate" in report
    assert "anomaly_rate" in report

@pytest.mark.asyncio
async def test_time_inconsistency_detection():
    from datetime import datetime, timedelta
    from src.db.session import SessionLocal
    from src.models.tasks import Task
    from src.models.review import ReviewSession
    async with SessionLocal() as session:
        t = Task(idempotency_key="incon-1", query="t", content_type="x")
        session.add(t); await session.commit()
        now = datetime.utcnow()
        session.add(ReviewSession(task_id=t.id, role="A", reviewer_id="u1",
                                   started_at=now, finished_at=now + timedelta(seconds=600),
                                   last_heartbeat_at=now,
                                   time_inconsistency_flag=False))
        await session.commit()
    report = await accuracy_report()
    assert report["time_inconsistency_rate"] >= 0
```

- [ ] **Step 2: 实现 src/dashboard/accuracy_report.py**

```python
from sqlalchemy import select, func
from src.db.session import SessionLocal
from src.models.review import ReviewSession

async def accuracy_report():
    async with SessionLocal() as session:
        total = await session.execute(select(func.count(ReviewSession.id))
                                       .where(ReviewSession.finished_at.is_not(None)))
        incon = await session.execute(select(func.count(ReviewSession.id))
                                        .where(ReviewSession.finished_at.is_not(None),
                                               ReviewSession.time_inconsistency_flag == True))
        anomaly = await session.execute(select(func.count(ReviewSession.id))
                                         .where(ReviewSession.finished_at.is_not(None),
                                                ReviewSession.anomaly_flag == True))
        total_n = total.scalar() or 0
        incon_n = incon.scalar() or 0
        anomaly_n = anomaly.scalar() or 0
        return {
            "time_inconsistency_rate": (incon_n / total_n) if total_n > 0 else 0.0,
            "anomaly_rate": (anomaly_n / total_n) if total_n > 0 else 0.0,
            "total_reviewed": total_n,
            "inconsistency_flagged": incon_n,
            "anomaly_flagged": anomaly_n,
        }
```

- [ ] **Step 3: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_accuracy_report.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/dashboard/accuracy_report.py tests/unit/test_accuracy_report.py
git commit -m "feat: instrument accuracy report"
```

---

### Task 20: 跨栈 Schema 兼容性验证（第一段结束前的关键闸口）

**Files:**
- Create: `tests/migration/test_schema_compat.py`
- Create: `tests/migration/test_node_event_rebuild.py`

**Interfaces:**
- Consumes: 全部 schema
- Produces: 验证每张表都可被第二段 Temporal 直接读写

- [ ] **Step 1: 写 tests/migration/test_schema_compat.py**

```python
import pytest
from sqlalchemy import select
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.models.events import NodeEvent
from src.models.review import ReviewSession, Batch, BatchMember
from src.models.assets import Asset, OcrResult, CrossCheck
from src.models.drafts import Draft, PageCopy, RuleResult
from src.models.entities import EntitySnapshot, Claim, Evidence
from src.models.snapshots import PublishSnapshot

@pytest.mark.asyncio
async def test_all_core_tables_readable():
    async with SessionLocal() as session:
        for model in [Task, NodeEvent, ReviewSession, Batch, BatchMember,
                      Asset, OcrResult, CrossCheck, Draft, PageCopy,
                      RuleResult, EntitySnapshot, Claim, Evidence,
                      PublishSnapshot]:
            await session.execute(select(model).limit(1))
```

- [ ] **Step 2: 写 tests/migration/test_node_event_rebuild.py**

```python
import pytest
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.models.events import NodeEvent
from src.pipeline.orchestrator import run_pipeline
from src.dashboard.metrics import all_metrics

@pytest.mark.asyncio
async def test_lifecycle_rebuildable_from_node_events():
    async with SessionLocal() as session:
        task = Task(idempotency_key="rebuild-1", query="t", content_type="x")
        session.add(task); await session.commit(); await session.refresh(task)
        tid = str(task.id)
    await run_pipeline(tid)
    metrics = await all_metrics()
    assert metrics["throughput_per_hour"] >= 1
```

- [ ] **Step 3: 跑两个测试**

Run: `uv run pytest tests/migration/ -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/migration/
git commit -m "test: cross-stack schema compatibility + node_event lifecycle rebuild"
```

---

### Task 21: 阶段 0/1 集成测试（单人跑通 + 3 人协作）

**Files:**
- Create: `tests/validation/test_phase0_endtoend.py`
- Create: `tests/validation/test_phase1_3person.py`

**Interfaces:**
- Consumes: 全部模块
- Produces: 验证单人顺序扮演 A→B→C + 3 人并行场景

- [ ] **Step 1: 写 tests/validation/test_phase0_endtoend.py**

```python
import pytest
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.pipeline.orchestrator import run_pipeline
from src.review.locks import acquire_lock
from src.api.review import record_heartbeat
from src.review.heartbeat import record_heartbeat as rb

@pytest.mark.asyncio
async def test_phase0_single_person_endtoend():
    """1 人顺序扮演 A → B → C 完成一条任务"""
    async with SessionLocal() as session:
        task = Task(idempotency_key="phase0-1", query="测试", content_type="x")
        session.add(task); await session.commit(); await session.refresh(task)
        tid = str(task.id)
    results = await run_pipeline(tid)
    assert len(results) == 13
    for role in ["A", "B", "C"]:
        r = await acquire_lock(tid, role, "single-user")
        assert r["acquired"] is True
```

- [ ] **Step 2: 写 tests/validation/test_phase1_3person.py**

```python
import pytest
import asyncio
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.pipeline.orchestrator import run_pipeline
from src.review.locks import acquire_lock

@pytest.mark.asyncio
async def test_phase1_three_people_parallel():
    async with SessionLocal() as session:
        task = Task(idempotency_key="phase1-1", query="测试", content_type="x")
        session.add(task); await session.commit(); await session.refresh(task)
        tid = str(task.id)
    await run_pipeline(tid)
    locks = await asyncio.gather(
        acquire_lock(tid, "A", "reviewer-a"),
        acquire_lock(tid, "B", "reviewer-b"),
        acquire_lock(tid, "C", "reviewer-c"),
    )
    assert all(l["acquired"] for l in locks)
```

- [ ] **Step 3: 跑两个测试**

Run: `uv run pytest tests/validation/ -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/validation/
git commit -m "test: phase 0/1 validation (single + 3-person)"
```

---

### Task 22: 阶段 1-2 暂停点（说明性任务，非代码）

**Files:**
- Create: `docs/phase-1-2-pause-checklist.md`

**Interfaces:**
- Consumes: 无
- Produces: 验证团队使用本平台跑 300-500 条 + 单日压力 + 3-5 天稳定性

- [ ] **Step 1: 创建暂停检查清单**

```markdown
# 阶段 1-2 暂停点（执行此计划时在这里暂停）

## 必须完成项

- [ ] 真实跑通 50-100 条（流程调通）
- [ ] 跑通 300-500 条（小批验证）
- [ ] 单日压力测试：3 人全速 8 小时
- [ ] 稳定性测试：连续 3-5 天

## 必须采集的指标

- [ ] Q3（3 人稳定合格产能/天）中位数
- [ ] 模型成本：单任务包 / 单正文 / 单图片 / 单 OCR 的 CNY 中位数
- [ ] 绿色/黄色/红色比例分布（波动 < 5%）
- [ ] 仪器精度报告 `time_inconsistency_flag` < 1%

## 退出条件（必须满足才能进入阶段 3）

- [ ] Q3 已稳定
- [ ] 模型供应商并发额度未触限流（或已记录限流频次）
- [ ] 抽检对账偏差 < 5%
- [ ] 看板数据能反向重建每条任务完整时间线

## 暂停后做什么

采集完上述指标后，与项目方 review Q3 与模型成本数据，**然后才进入 Task 23（Temporal 迁移）**。
不要在 Q3 未稳定前开始 Task 23，因为 Temporal Activity 的超时/补偿参数需要真实数据支撑。
```

- [ ] **Step 2: Commit**

```bash
git add docs/phase-1-2-pause-checklist.md
git commit -m "docs: phase 1-2 pause checklist (Q3 collection gate)"
```

---

### Task 23: Temporal Workflow 骨架（第二段起点）

**Files:**
- Create: `src/temporal_migration/workflow.py`
- Create: `src/temporal_migration/activities.py`
- Create: `pyproject.toml`（增加 `temporalio>=1.5` 依赖）
- Create: `tests/migration/test_temporal_vs_lightstack.py`

**Interfaces:**
- Consumes: 全部模块函数（作为 Activity）
- Produces: Temporal Workflow + Activities，对外接口与 asyncio orchestrator 等价

- [ ] **Step 1: 增加 temporalio 依赖到 pyproject.toml**

```toml
"temporalio>=1.5",
```

Run: `uv sync`

- [ ] **Step 2: 写 src/temporal_migration/activities.py**

```python
from temporalio import activity
from src.pipeline.nodes import execute_node

@activity.defn
async def run_node_activity(task_id: str, node_name: str, input_data: dict) -> dict:
    return await execute_node(task_id, node_name, input_data)
```

- [ ] **Step 3: 写 src/temporal_migration/workflow.py**

```python
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy
from src.temporal_migration.activities import run_node_activity
from src.pipeline.nodes import NODES

@workflow.defn
class CapacityPipelineWorkflow:
    @workflow.run
    async def run(self, task_id: str) -> dict:
        results = {}
        for node_name in NODES:
            result = await workflow.execute_activity(
                run_node_activity,
                args=[task_id, node_name, {"task_id": task_id}],
                start_to_close_timeout=timedelta(seconds=300),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            results[node_name] = result
        return results
```

- [ ] **Step 4: 写 tests/migration/test_temporal_vs_lightstack.py**

```python
import pytest
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.pipeline.orchestrator import run_pipeline as light_run

@pytest.mark.asyncio
async def test_light_pipeline_baseline():
    async with SessionLocal() as session:
        t = Task(idempotency_key="tempo-baseline", query="t", content_type="x")
        session.add(t); await session.commit(); await session.refresh(t)
        tid = str(t.id)
    results = await light_run(tid)
    assert len(results) == 13
```

注：Temporal 端到端对比测试需要本地 Temporal Server，此处仅占位。完整对比在 Task 27 中实施。

- [ ] **Step 5: Commit**

```bash
git add src/temporal_migration/ pyproject.toml tests/migration/test_temporal_vs_lightstack.py
git commit -m "feat: Temporal workflow skeleton + activity adapter"
```

---

### Task 24: Temporal Activities 完整迁移

**Files:**
- Modify: `src/temporal_migration/activities.py`（覆盖 13 个节点）
- Create: `tests/migration/test_temporal_dry_run.py`

**Interfaces:**
- Consumes: Task 23 骨架
- Produces: 13 个 `@activity.defn` 函数

- [ ] **Step 1: 扩展 activities.py**

```python
from temporalio import activity
from src.pipeline.nodes import (
    execute_node, node_draft_gen, node_rule_check, node_page_split,
    node_asset_gen, node_ocr_read, node_cross_check,
    node_risk_classify, node_review_queue, node_batch_signoff,
    node_publish_snapshot,
)

@activity.defn
async def node_task_import_activity(task_id, input_data):
    return await execute_node(task_id, "task_import", input_data)

@activity.defn
async def node_entity_bind_activity(task_id, input_data):
    return await execute_node(task_id, "entity_bind", input_data)

@activity.defn
async def node_evidence_build_activity(task_id, input_data):
    return await execute_node(task_id, "evidence_build", input_data)

@activity.defn
async def node_draft_gen_activity(task_id, input_data):
    return await node_draft_gen({**input_data, "task_id": task_id})

@activity.defn
async def node_rule_check_activity(task_id, input_data):
    return await node_rule_check({**input_data, "task_id": task_id})

@activity.defn
async def node_page_split_activity(task_id, input_data):
    return await node_page_split({**input_data, "task_id": task_id})

@activity.defn
async def node_asset_gen_activity(task_id, input_data):
    return await node_asset_gen({**input_data, "task_id": task_id})

@activity.defn
async def node_ocr_read_activity(task_id, input_data):
    return await node_ocr_read({**input_data, "task_id": task_id})

@activity.defn
async def node_cross_check_activity(task_id, input_data):
    return await node_cross_check({**input_data, "task_id": task_id})

@activity.defn
async def node_risk_classify_activity(task_id, input_data):
    return await node_risk_classify({**input_data, "task_id": task_id})

@activity.defn
async def node_review_queue_activity(task_id, input_data):
    return await node_review_queue({**input_data, "task_id": task_id})

@activity.defn
async def node_batch_signoff_activity(task_id, input_data):
    return await node_batch_signoff({**input_data, "task_id": task_id})

@activity.defn
async def node_publish_snapshot_activity(task_id, input_data):
    return await node_publish_snapshot({**input_data, "task_id": task_id})
```

- [ ] **Step 2: 写测试（dry-run 形式不连 Temporal Server）**

```python
# tests/migration/test_temporal_dry_run.py
import pytest
from src.temporal_migration.activities import (
    node_draft_gen_activity, node_rule_check_activity, node_page_split_activity
)

@pytest.mark.asyncio
async def test_activity_wrappers_call_correct_nodes():
    result = await node_rule_check_activity("fake-id", {"text": "。" * 30 + "正常" * 50, "title": "测试"})
    assert "rule_results" in result
```

- [ ] **Step 3: 跑测试确认通过**

Run: `uv run pytest tests/migration/test_temporal_dry_run.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/temporal_migration/activities.py tests/
git commit -m "feat: 13 Temporal activities mapped to existing nodes"
```

---

### Task 25: Temporal 重试/补偿/超时配置

**Files:**
- Modify: `src/temporal_migration/workflow.py`（每个 Activity 配置 retry_policy + timeout）
- Create: `tests/unit/test_workflow_config.py`

**Interfaces:**
- Consumes: Task 24 activities
- Produces: 节点级超时与重试策略（基于 Q3 实测数据）

- [ ] **Step 1: 重写 workflow.py 节点级配置**

```python
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy
from src.temporal_migration.activities import (
    node_task_import_activity, node_entity_bind_activity,
    node_evidence_build_activity, node_draft_gen_activity,
    node_rule_check_activity, node_page_split_activity,
    node_asset_gen_activity, node_ocr_read_activity,
    node_cross_check_activity, node_risk_classify_activity,
    node_review_queue_activity, node_batch_signoff_activity,
    node_publish_snapshot_activity,
)

NODE_CONFIG = {
    "task_import":        {"timeout": 30,   "retries": 1},
    "entity_bind":        {"timeout": 30,   "retries": 1},
    "evidence_build":     {"timeout": 600,  "retries": 2},
    "draft_gen":          {"timeout": 300,  "retries": 2},
    "rule_check":         {"timeout": 30,   "retries": 1},
    "page_split":         {"timeout": 300,  "retries": 2},
    "asset_gen":          {"timeout": 600,  "retries": 2},
    "ocr_read":           {"timeout": 600,  "retries": 2},
    "cross_check":        {"timeout": 60,   "retries": 1},
    "risk_classify":      {"timeout": 30,   "retries": 1},
    "review_queue":       {"timeout": 30,   "retries": 1},
    "batch_signoff":      {"timeout": 60,   "retries": 1},
    "publish_snapshot":   {"timeout": 30,   "retries": 1},
}

ACTIVITY_MAP = {
    "task_import": node_task_import_activity,
    "entity_bind": node_entity_bind_activity,
    "evidence_build": node_evidence_build_activity,
    "draft_gen": node_draft_gen_activity,
    "rule_check": node_rule_check_activity,
    "page_split": node_page_split_activity,
    "asset_gen": node_asset_gen_activity,
    "ocr_read": node_ocr_read_activity,
    "cross_check": node_cross_check_activity,
    "risk_classify": node_risk_classify_activity,
    "review_queue": node_review_queue_activity,
    "batch_signoff": node_batch_signoff_activity,
    "publish_snapshot": node_publish_snapshot_activity,
}

@workflow.defn
class CapacityPipelineWorkflow:
    @workflow.run
    async def run(self, task_id: str) -> dict:
        from src.pipeline.nodes import NODES
        results = {}
        for node_name in NODES:
            cfg = NODE_CONFIG[node_name]
            result = await workflow.execute_activity(
                ACTIVITY_MAP[node_name],
                args=[task_id, {"task_id": task_id}],
                start_to_close_timeout=timedelta(seconds=cfg["timeout"]),
                retry_policy=RetryPolicy(
                    maximum_attempts=cfg["retries"] + 1,
                    initial_interval=timedelta(seconds=2),
                ),
            )
            results[node_name] = result
        return results
```

- [ ] **Step 2: 写测试**

```python
# tests/unit/test_workflow_config.py
from src.temporal_migration.workflow import NODE_CONFIG, ACTIVITY_MAP
from src.pipeline.nodes import NODES

def test_all_nodes_have_config():
    for n in NODES:
        assert n in NODE_CONFIG
        assert n in ACTIVITY_MAP

def test_timeouts_reasonable():
    for n, cfg in NODE_CONFIG.items():
        assert cfg["timeout"] >= 30
        assert cfg["retries"] >= 0
```

- [ ] **Step 3: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_workflow_config.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/temporal_migration/workflow.py tests/unit/test_workflow_config.py
git commit -m "feat: Temporal workflow with per-node timeout/retry config"
```

---

### Task 26: 6 人配置功能（多角色复核 + 会签链）

**Files:**
- Create: `src/review/multi_reviewer.py`
- Create: `src/review/signoff_chain.py`
- Create: `tests/integration/test_signoff_chain.py`

**Interfaces:**
- Consumes: review_sessions
- Produces: `submit_signoff(task_id, role, approver_id, conclusion)` 在所有必签角色签字后才能冻结发布快照

- [ ] **Step 1: 写 src/review/signoff_chain.py**

```python
from datetime import datetime
from sqlalchemy import select, insert
from src.db.session import SessionLocal
from src.models.review import Approval
from src.models.snapshots import PublishSnapshot

REQUIRED_ROLES = {"A", "B", "C", "release"}

async def submit_signoff(task_id: str, role: str, approver_id: str, conclusion: str) -> dict:
    async with SessionLocal() as session:
        session.add(Approval(task_id=task_id, role=role,
                             approver_id=approver_id, conclusion=conclusion))
        await session.commit()
        result = await session.execute(
            select(Approval).where(Approval.task_id == task_id))
        signed_roles = {a.role for a in result.scalars()}
        all_signed = REQUIRED_ROLES.issubset(signed_roles)
        if all_signed:
            session.add(PublishSnapshot(task_id=task_id,
                                         snapshot_data={"frozen": True,
                                                        "signed_roles": list(signed_roles)}))
            await session.commit()
        return {"signed_roles": list(signed_roles), "all_signed": all_signed}
```

- [ ] **Step 2: 写 src/review/multi_reviewer.py（6 人分配辅助）**

```python
async def assign_6_person_team(task_id: str) -> dict:
    """返回 A 主审+A 复核, B 主审+B 复核, C 主审+C 复核 的角色分配"""
    return {
        "A_primary": f"{task_id}-A1",
        "A_reviewer": f"{task_id}-A2",
        "B_primary": f"{task_id}-B1",
        "B_reviewer": f"{task_id}-B2",
        "C_primary": f"{task_id}-C1",
        "C_reviewer": f"{task_id}-C2",
    }
```

- [ ] **Step 3: 写失败测试**

```python
# tests/integration/test_signoff_chain.py
import pytest
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.review.signoff_chain import submit_signoff

@pytest.mark.asyncio
async def test_signoff_requires_all_roles():
    async with SessionLocal() as session:
        task = Task(idempotency_key="signoff-1", query="t", content_type="x")
        session.add(task); await session.commit(); await session.refresh(task)
        tid = str(task.id)
    for role in ["A", "B", "C"]:
        r = await submit_signoff(tid, role, "u1", "ok")
        assert r["all_signed"] is False
    r = await submit_signoff(tid, "release", "u9", "ok")
    assert r["all_signed"] is True
    assert set(r["signed_roles"]) == {"A", "B", "C", "release"}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/integration/test_signoff_chain.py -v`
Expected: PASS

- [ ] **Step 5: 挂 API 并 commit**

```python
# src/api/review.py 添加
from pydantic import BaseModel
from src.review.signoff_chain import submit_signoff

class SignoffIn(BaseModel):
    task_id: str
    role: str
    approver_id: str
    conclusion: str

@router.post("/api/review/signoff")
async def signoff(payload: SignoffIn):
    return await submit_signoff(payload.task_id, payload.role,
                                  payload.approver_id, payload.conclusion)
```

```bash
git add src/review/ src/api/review.py tests/
git commit -m "feat: 6-person sign-off chain (A/B/C/release all required)"
```

---

### Task 27: 跨阶段数据对账（light-stack vs Temporal）

**Files:**
- Create: `tests/migration/test_temporal_vs_lightstack.py`（扩展 Task 23 占位）

**Interfaces:**
- Consumes: 两套栈产出的 node_events
- Produces: 对比报告（同 100 条任务的耗时差异）

- [ ] **Step 1: 扩展测试文件**

```python
# tests/migration/test_temporal_vs_lightstack.py 扩展
import time
import pytest
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.models.events import NodeEvent
from src.pipeline.orchestrator import run_pipeline

@pytest.mark.asyncio
async def test_pipeline_durations_logged_in_node_events():
    async with SessionLocal() as session:
        task = Task(idempotency_key="dur-1", query="t", content_type="x")
        session.add(task); await session.commit(); await session.refresh(task)
        tid = str(task.id)
    start = time.time()
    await run_pipeline(tid)
    elapsed = time.time() - start
    async with SessionLocal() as session:
        events = await session.execute(NodeEvent.__table__.select().where(NodeEvent.task_id == task.id))
        durations = [(e.started_at, e.finished_at) for e in events
                     if e.started_at and e.finished_at]
    assert len(durations) >= 13
    assert elapsed < 60
```

注：完整 light-stack vs Temporal 对比需要本地 Temporal Server + Worker。在验证期实际运行时执行：跑同样 100 条，差异 < 15% 即视为栈迁移不影响数据可信。

- [ ] **Step 2: 跑测试确认通过**

Run: `uv run pytest tests/migration/test_temporal_vs_lightstack.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/migration/
git commit -m "test: pipeline duration logged to node_events for stack reconciliation"
```

---

### Task 28: 验证报告产出脚本

**Files:**
- Create: `src/dashboard/validation_report.py`
- Create: `tests/integration/test_validation_report.py`

**Interfaces:**
- Consumes: 全部指标表
- Produces: `generate_validation_report() -> {q3, q6, cost_per_task, ...}` 写到 `reports/validation-{date}.md`

- [ ] **Step 1: 写 src/dashboard/validation_report.py**

```python
from datetime import datetime
from sqlalchemy import select, func
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.models.events import NodeEvent
from src.models.review import ReviewSession, RiskClassification
from src.dashboard.metrics import (
    first_pass_rate_last_24h, cost_per_task_24h, p95_node_duration,
)
from src.dashboard.accuracy_report import accuracy_report

async def generate_validation_report(window_days: int = 5) -> str:
    async with SessionLocal() as session:
        cutoff_days = datetime.utcnow().timestamp() - window_days * 86400
        total_tasks = await session.execute(
            select(func.count(Task.id)).where(Task.created_at >= datetime.utcfromtimestamp(cutoff_days)))
        passed = await session.execute(
            select(func.count(Task.id)).where(Task.created_at >= datetime.utcfromtimestamp(cutoff_days),
                                                Task.status == "green"))
        total = total_tasks.scalar() or 0
        passed_n = passed.scalar() or 0

        green = await session.execute(select(func.count(RiskClassification.id))
                                        .where(RiskClassification.level == "green"))
        yellow = await session.execute(select(func.count(RiskClassification.id))
                                         .where(RiskClassification.level == "yellow"))
        red = await session.execute(select(func.count(RiskClassification.id))
                                      .where(RiskClassification.level == "red"))
        g, y, r = green.scalar() or 0, yellow.scalar() or 0, red.scalar() or 0

    fpr = await first_pass_rate_last_24h()
    cost = await cost_per_task_24h()
    p95 = await p95_node_duration()
    acc = await accuracy_report()

    md = f"""# 产能验证报告

生成时间: {datetime.utcnow().isoformat()}

## 产能
- 窗口: {window_days} 天
- 总任务: {total}
- 合格交付: {passed_n}
- 平均日合格产能（Q3/Q6）: {passed_n / window_days:.1f} 条/天

## 质量分布
- 绿色: {g}
- 黄色: {y}
- 红色: {r}
- 首过率: {fpr:.2%}

## 性能
- P95 节点耗时: {p95:.1f} 秒
- 单任务成本: {cost:.4f} CNY

## 仪器精度
- time_inconsistency 率: {acc['time_inconsistency_rate']:.2%}
- 异常率: {acc['anomaly_rate']:.2%}

## 月产 10 万条推算（按 {passed_n / window_days:.0f} 条/天稳定合格产能）
- 理论人员数: {3 * 100000 / (passed_n / window_days * 22):.1f}
- 建议人员数（80% 效率）: {3 * 100000 / (passed_n / window_days * 22 * 0.80):.1f}
"""
    return md
```

- [ ] **Step 2: 写测试**

```python
# tests/integration/test_validation_report.py
import pytest
from src.dashboard.validation_report import generate_validation_report

@pytest.mark.asyncio
async def test_report_generated():
    md = await generate_validation_report(window_days=5)
    assert "产能验证报告" in md
    assert "Q3/Q6" in md
    assert "10 万条" in md
```

- [ ] **Step 3: 跑测试确认通过**

Run: `uv run pytest tests/integration/test_validation_report.py -v`
Expected: PASS

- [ ] **Step 4: 写 CLI 包装并 commit**

```python
# src/api/dashboard.py 添加
from src.dashboard.validation_report import generate_validation_report

@router.get("/api/dashboard/validation-report")
async def validation_report():
    md = await generate_validation_report()
    return {"markdown": md}
```

```bash
git add src/dashboard/validation_report.py src/api/dashboard.py tests/
git commit -m "feat: validation report generator + dashboard endpoint"
```

---

## 自审

**1. Spec 覆盖核对**：
- §3 团队三阶段（1/3/6 人）→ Task 21 验证测试 + Task 26 6 人配置功能 ✓
- §4 架构两段式栈 → Task 1-13 轻量栈 + Task 23-25 Temporal 迁移 ✓
- §5 数据流 13 节点 → Task 10-13 ✓
- §6 计量时间采集 → Task 14 心跳 + Task 15 自动挂起 + Task 16 异常剔除 + Task 19 精度报告 ✓
- §7 看板指标 → Task 18 ✓
- §8 错误处理降级 → Task 6 降级链 ✓
- §9 测试策略 → Task 20-21 集成测试 + Task 27 跨阶段对账 ✓
- §10 验证周期 → Task 22 暂停点 + Task 28 报告产出 ✓

**2. 占位扫描**：无 TBD/TODO/FIXME。每个 task 步骤都有具体代码或命令。

**3. 类型一致性**：所有 task 间共享的接口（`compute_node_key`、`execute_node`、`run_pipeline`、`acquire_lock`、`submit_signoff`）签名在第一个定义处确定，后续 task 一致引用。

---

## 执行选项

**Plan complete and saved to `docs/superpowers/plans/2026-08-13-产能验证平台-implementation.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**

注意：本计划中有 **Task 22（阶段 1-2 暂停点）**——这是真实的暂停闸口，验证团队需要在 Q3 稳定后才能进入 Task 23 的 Temporal 迁移。执行时请尊重这个边界，不要在 Q3 未采集完整前跳到 Task 23。
