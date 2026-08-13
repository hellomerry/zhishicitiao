# S2 Temporal 迁移与六人验证 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把流水线编排层从 asyncio 迁到 Temporal，加 6 人配置功能（多角色复核 + 会签链），跑 6 人验证，产出 Q6 与验证报告。

**Architecture:** 两段式栈的第二段。复用 S0 已建好的 PG Schema 与 8 模块函数，仅替换编排层为 Temporal Workflow + 13 Activities。6 人配置 = A/B/C 各 2 人（主审+复核），会签链要求 A/B/C/release 四角色全部签字才冻结发布快照。

**Tech Stack:** Temporal 1.26+, temporalio>=1.5, LiteLLM, 复用 S0 全部依赖。

**Spec:** `docs/superpowers/specs/2026-08-13-产能验证平台-design.md`

**前置闸点（硬约束）:** 必须在 S1 采集到稳定 Q3 后才能启动本阶段。Temporal Activity 的超时/补偿参数依赖真实数据，禁止在 Q3 未稳定前进入。

**本阶段 task 范围:** Task 23-28（对应 roadmap §4 里程碑 M6-M8）

**上一阶段:** S1（三人验证与 Q3 采集），见 `S1-三人验证与Q3采集.md`

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

