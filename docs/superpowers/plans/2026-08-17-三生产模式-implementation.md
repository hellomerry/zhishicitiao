# 三生产模式（对比/单品/通用）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「单套通用提示词 + 纯文生图」升级为「按 mode 选提示词 + 图生图（对比/单品）+ 文生图（通用）」，生图模型切到 gpt-image-1.5。

**Architecture:** 在 `tasks` 表加 `mode` 字段（compare/single/general，导入时手动指定），提示词系统按 `(用途, mode)` 提供 6 条提示词，`draft_gen`/`entity_bind`/`asset_gen` 三个节点按 mode 分支，生图网关重写为 gpt-image-1.5（文生图 `/generations` + 图生图 `/edits`）。

**Tech Stack:** FastAPI + SQLAlchemy(async) + asyncpg + httpx + pytest(asyncio_mode=auto) + gpt-image-1.5（OpenAI 兼容 Images API，经转发机）。

**Spec:** `docs/superpowers/specs/2026-08-17-三生产模式-design.md`

## Global Constraints

- `mode` 取值固定为 `compare` / `single` / `general`（默认 `general`，向后兼容旧数据）。
- 生图模型固定 `gpt-image-1.5`，尺寸 `1024x1536`（3:4 竖版）。
- 图生图参数 `input_fidelity="high"`。
- 对比/单品模式参考图来源：`entity_bind` 存的 `source_type="official"` 资产。
- 图生图参考图下载失败时降级文生图（同模式提示词、去掉参考图）。
- 迁移必须幂等（`IF NOT EXISTS`），重复执行不报错。
- 测试约定：外部调用（生图/搜图/搜索/文本模型）一律 mock，不真实调 API；测试库 `qvp_test` 隔离。

---

## Task 1: `tasks` 加 `mode` 列 + Task 模型

**Files:**
- Modify: `migrations/001_initial_schema.sql`（tasks 建表后）
- Modify: `src/models/tasks.py`

**Interfaces:**
- Produces: `Task.mode: Text`（Python 侧默认 `"general"`，DB 侧 `DEFAULT 'general'`）。后续所有节点读 `task.mode or "general"`。

- [ ] **Step 1: 写迁移 SQL**

在 `001_initial_schema.sql` 的 `tasks` 索引（`tasks_created_at_idx`）之后、`entity_snapshots` 建表之前，插入一行：

```sql
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'general';
```

- [ ] **Step 2: Task 模型加字段**

`src/models/tasks.py` 的 `Task` 类，`content_type` 之后加：

```python
    mode = Column(Text, nullable=False, default="general")
```

- [ ] **Step 3: 跑迁移 + 冒烟验证**

```bash
psql -h localhost -U qvp -d qvp -f migrations/001_initial_schema.sql
psql -h localhost -U qvp -d qvp -c "\d tasks"
```
Expected: `tasks` 表出现 `mode` 列，默认值 `'general'`。

- [ ] **Step 4: 写迁移幂等测试**

`tests/migration/test_mode_column.py`：

```python
import pytest
import asyncpg


@pytest.mark.asyncio
async def test_tasks_has_mode_column():
    conn = await asyncpg.connect("postgresql://qvp:qvp@localhost:5432/qvp_test")
    try:
        col = await conn.fetchrow(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name='tasks' AND column_name='mode'")
    finally:
        await conn.close()
    assert col is not None
    assert "general" in (col["column_default"] or "")
```

- [ ] **Step 5: 跑测试**

```bash
pytest tests/migration/test_mode_column.py -v
```
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add migrations/001_initial_schema.sql src/models/tasks.py tests/migration/test_mode_column.py
git commit -m "feat: tasks 加 mode 列（compare/single/general）"
```

---

## Task 2: 提示词系统按 mode 提供 6 条提示词

**Files:**
- Modify: `src/gateway/prompt_versions.py`
- Test: `tests/unit/test_prompt_versions.py`

**Interfaces:**
- Produces:
  - `get_draft_prompt(mode: str) -> str`（mode 未知时回退 `general`）
  - `get_image_prompt(mode: str, page_body: str) -> str`（`{page_body}` 占位符替换）
  - 保留旧 `get_prompt(name, version)`（`draft_v1`/`page_split_v1`/`evidence_v1` 仍可读，避免破坏旧引用）

- [ ] **Step 1: 写失败测试**

`tests/unit/test_prompt_versions.py`：

```python
from src.gateway.prompt_versions import get_draft_prompt, get_image_prompt


def test_draft_prompt_per_mode():
    assert "对比" in get_draft_prompt("compare")
    assert "单品" in get_draft_prompt("single")
    assert "图文内容" in get_draft_prompt("general")


def test_draft_prompt_unknown_mode_falls_back_to_general():
    assert get_draft_prompt("nope") == get_draft_prompt("general")


def test_image_prompt_fills_page_body():
    p = get_image_prompt("general", "本页是测试文案")
    assert "本页是测试文案" in p
    assert "{page_body}" not in p


def test_image_prompt_per_mode():
    assert "两个主体" in get_image_prompt("compare", "")
    assert "参考实景图" in get_image_prompt("single", "")
    assert "无参考图" in get_image_prompt("general", "")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/unit/test_prompt_versions.py -v
```
Expected: FAIL（`ImportError: cannot import name 'get_draft_prompt'`）

- [ ] **Step 3: 重写 `prompt_versions.py`**

```python
"""提示词版本库：按 (用途, mode) 提供正文/生图提示词。"""

_SHARED_IMAGE_STYLE = (
    "竖版3:4图文卡片，一级/二级标题与正文字号对应。"
    "主体清晰不被遮挡、展现完整主体不裁剪关键特征。"
    "坚韧治愈风、高清、极简高级，背景不太白也不太暗。"
    "所有文字必须使用标准可读中文黑体，禁止艺术化变形、阴影、描边、透视扭曲，"
    "正文统一基线对齐、可印刷级清晰；图中文字不超过100字，不出现字号过小的文字，"
    "不要出现「封面/第X页」等字样。排版不要模板化（每页排版不同），"
    "图片元素不与前页重复。不出现人脸、书籍等元素，尽量不出现带文字的物体。"
)

DRAFT_PROMPTS = {
    "general": "请你以小红书博主的写作风格及模式，结合权威可靠信源的数据库，创作一篇图文内容。要求：简洁清晰、结构完整、总分总结构、每段加小标题、400-700字、无绝对化表述、无emoji、中文标点。",
    "single": "请你以小红书博主的写作风格，结合权威可靠信源的数据库，创作一篇单品深度测评图文。围绕单一产品/事物展开，依次讲透：它是什么、原理或关键参数、实测体验、优点、局限、安全/使用提醒、适合谁。要求：简洁清晰、总分总结构、每段加小标题、400-700字、无绝对化表述、无emoji、中文标点，事实数据需有信源支撑。",
    "compare": "请你以小红书博主的写作风格，结合权威可靠信源的数据库，创作一篇对比类图文。客观对比两个主体（产品/学校/方案等），平分笔墨，逐维度列出各自的事实参数、优劣与适用场景，最后给出取舍建议。要求：简洁清晰、总分总结构、每段加小标题、400-700字、无绝对化表述、无emoji、中文标点，事实数据需有信源支撑，不偏袒任何一方。",
}

IMAGE_PROMPTS = {
    "general": "通用科普/教程配图，纯 AI 生成、无参考图。" + _SHARED_IMAGE_STYLE + "本页文案：{page_body}",
    "single": "单品评测配图，将提供的参考实景图融入画面：去水印、去人物、实景图不重复、每页实景图不宜过多以免杂乱；不删减参考图上的文字，也不额外添加其他图片。" + _SHARED_IMAGE_STYLE + "本页文案：{page_body}",
    "compare": "对比类配图，将两个主体的参考实景图融入画面，每页尽量同时呈现两个主体做对比（参考图顺序不能乱）：去水印、去人物、实景图不重复。" + _SHARED_IMAGE_STYLE + "本页文案：{page_body}",
}

# 旧版提示词（保留兼容：get_prompt 仍可读 draft_v1 / page_split_v1 / evidence_v1）
PROMPT_VERSIONS = {
    "draft_v1": DRAFT_PROMPTS["general"],
    "page_split_v1": "对文章进行精简和拆分，总文字严格控制到350字以内，包括封面和每一页的文字内容，适合放在图上，每个部分一段话。",
    "evidence_v1": "提取这段话中可验证的事实点（数值、单位、年份、定义、引用、因果），每个事实点标注风险等级。",
}


def get_prompt(name: str, version: str = None) -> str:
    if version:
        key = f"{name}_{version}"
        if key in PROMPT_VERSIONS:
            return PROMPT_VERSIONS[key]
    return PROMPT_VERSIONS[f"{name}_v1"]


def get_draft_prompt(mode: str) -> str:
    return DRAFT_PROMPTS.get(mode, DRAFT_PROMPTS["general"])


def get_image_prompt(mode: str, page_body: str) -> str:
    template = IMAGE_PROMPTS.get(mode, IMAGE_PROMPTS["general"])
    return template.replace("{page_body}", page_body)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/unit/test_prompt_versions.py -v
```
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add src/gateway/prompt_versions.py tests/unit/test_prompt_versions.py
git commit -m "feat: 提示词按 mode 提供 6 条（正文+生图）"
```

---

## Task 3: 导入流程加 mode（API + CSV + 前端）

**Files:**
- Modify: `src/api/tasks.py`（`ImportQueriesIn`、`import_queries`、`import`）
- Modify: `static/import.html`

**Interfaces:**
- Consumes: `Task.mode`（Task 1）
- Produces: `POST /api/tasks/import_queries` 接受 `mode`；`POST /api/tasks/import` 读 CSV `mode` 列；幂等键含 mode。

- [ ] **Step 1: 写失败测试**

`tests/integration/test_import_mode.py`：

```python
import uuid
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from src.api.main import app
from src.db.session import SessionLocal
from src.models.tasks import Task


@pytest.mark.asyncio
async def test_import_queries_with_mode():
    uniq = uuid.uuid4().hex[:8]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post("/api/tasks/import_queries",
                             json={"queries": [f"对比_{uniq}"], "mode": "compare"})
    assert resp.status_code == 200
    async with SessionLocal() as session:
        task = (await session.execute(
            select(Task).where(Task.query == f"对比_{uniq}"))).scalar_one()
        assert task.mode == "compare"


@pytest.mark.asyncio
async def test_import_csv_reads_mode_column():
    uniq = uuid.uuid4().hex[:8]
    csv = f"query,content_type,mode\n单品_{uniq},product,single\n"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post("/api/tasks/import",
                             files={"file": ("t.csv", csv.encode(), "text/csv")})
    assert resp.status_code == 200
    async with SessionLocal() as session:
        task = (await session.execute(
            select(Task).where(Task.query == f"单品_{uniq}"))).scalar_one()
        assert task.mode == "single"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/integration/test_import_mode.py -v
```
Expected: FAIL（`task.mode` 报 `AttributeError` 或断言失败——先改 `tasks.py` 才能读 mode）

- [ ] **Step 3: 改 `src/api/tasks.py`**

`ImportQueriesIn` 加 `mode`：

```python
class ImportQueriesIn(BaseModel):
    queries: list[str]        # 每行一个 Query
    content_type: str = "generic"   # 内容类型：generic / school / product / compare
    mode: str = "general"     # 生产模式：compare / single / general
```

`import_queries` 的 key 与 Task 构造加 mode：

```python
            key = f"{q}|{payload.content_type}|{payload.mode}"
            ...
            task = Task(idempotency_key=key, query=q,
                        content_type=payload.content_type, mode=payload.mode,
                        status="draft")
```

`import`（CSV）读 mode 并入 key：

```python
                mode = (row.get("mode") or "general").strip()
                key = f"{row['query']}|{row['content_type']}|{row.get('platform', '')}|{mode}"
                ...
                task = Task(
                    idempotency_key=key,
                    query=row["query"],
                    content_type=row["content_type"],
                    platform=row.get("platform"),
                    mode=mode,
                    status="draft",
                )
```

- [ ] **Step 4: 改 `static/import.html`**

把 `<select id="ctype">` 替换为生产模式下拉，`submit()` 里 `content_type` 改为 `mode`：

```html
    <label>生产模式</label>
    <select id="mode">
      <option value="general">通用（直接 AI 生图）</option>
      <option value="single">单品（实景图 → 生图）</option>
      <option value="compare">对比（实景图 → 生图）</option>
    </select>
```

```javascript
  const mode=document.getElementById('mode').value;
  ...
  body:JSON.stringify({queries,mode})
```

- [ ] **Step 5: 跑测试确认通过**

```bash
pytest tests/integration/test_import_mode.py -v
```
Expected: 2 PASS

- [ ] **Step 6: 跑导入回归**

```bash
pytest tests/integration/test_task_import.py -v
```
Expected: 仍 PASS（CSV 无 mode 列时默认 general）

- [ ] **Step 7: Commit**

```bash
git add src/api/tasks.py static/import.html tests/integration/test_import_mode.py
git commit -m "feat: 导入流程加 mode 字段（API + CSV + 前端）"
```

---

## Task 4: `node_draft_gen` 按 mode 选正文提示词

**Files:**
- Modify: `src/pipeline/nodes.py`（`node_draft_gen`）
- Test: `tests/integration/test_draft_gen_mode.py`

**Interfaces:**
- Consumes: `get_draft_prompt(mode)`（Task 2）、`Task.mode`（Task 1）
- Produces: `node_draft_gen` 返回的 `prompt_version` 为 `draft_{mode}_v1`；落库 `Draft.prompt_version` 同值。

- [ ] **Step 1: 写失败测试**

`tests/integration/test_draft_gen_mode.py`：

```python
import uuid
import pytest
from unittest.mock import patch
from sqlalchemy import select
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.models.drafts import Draft
from src.pipeline.nodes import node_draft_gen

FAKE_DRAFT = {"text": "这是测试正文。" * 60, "model_version": "deepseek/deepseek-v4-pro",
              "cost_cny": 0.01, "degraded": False}


@pytest.mark.asyncio
async def test_draft_gen_uses_compare_prompt():
    async with SessionLocal() as session:
        task = Task(idempotency_key=f"d-{uuid.uuid4().hex[:8]}", query="A vs B",
                    content_type="compare", mode="compare")
        session.add(task)
        await session.commit()
        await session.refresh(task)
        tid = task.id
    with patch("src.pipeline.nodes.call_with_failover", return_value=FAKE_DRAFT):
        out = await node_draft_gen({"task_id": tid})
    assert out["prompt_version"] == "draft_compare_v1"
    async with SessionLocal() as session:
        d = (await session.execute(
            select(Draft).where(Draft.task_id == tid))).scalar_one()
        assert d.prompt_version == "draft_compare_v1"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/integration/test_draft_gen_mode.py -v
```
Expected: FAIL（`prompt_version` 仍是 `draft_v1`）

- [ ] **Step 3: 改 `node_draft_gen`**

`src/pipeline/nodes.py` 里 `node_draft_gen`：

```python
async def node_draft_gen(input_data: dict) -> dict:
    from src.models.tasks import Task
    from src.models.drafts import Draft
    from src.gateway.prompt_versions import get_draft_prompt
    async with SessionLocal() as session:
        task = (await session.execute(
            select(Task).where(Task.id == input_data["task_id"]))).scalar_one()
        query = task.query
        mode = task.mode or "general"
    prompt = get_draft_prompt(mode) + "\n\n" + query
    prompt_version = f"draft_{mode}_v1"
    result = await call_with_failover(prompt, DEEPSEEK_MODEL, KIMI_MODEL)
    async with SessionLocal() as session:
        session.add(Draft(
            task_id=input_data["task_id"], version=1, body=result["text"],
            model_version=result["model_version"], prompt_version=prompt_version))
        await session.commit()
    return {"text": result["text"], "model_version": result["model_version"],
            "prompt_version": prompt_version, "cost_cny": result["cost_cny"],
            "degraded": result["degraded"]}
```

（顶部 `from src.gateway.prompt_versions import get_prompt` 可删除，因为已无调用。）

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/integration/test_draft_gen_mode.py -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/nodes.py tests/integration/test_draft_gen_mode.py
git commit -m "feat: draft_gen 按 mode 选正文提示词"
```

---

## Task 5: `node_entity_bind` 在 general 模式跳过搜图

**Files:**
- Modify: `src/pipeline/nodes.py`（`node_entity_bind`）
- Test: `tests/integration/test_entity_bind_mode.py`

**Interfaces:**
- Consumes: `Task.mode`（Task 1）
- Produces: `node_entity_bind` 在 general 模式返回 `{"searched_images": 0}` 且不写 official 资产。

- [ ] **Step 1: 写失败测试**

`tests/integration/test_entity_bind_mode.py`：

```python
import uuid
import pytest
from unittest.mock import patch
from sqlalchemy import select, func
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.models.assets import Asset
from src.pipeline.nodes import node_entity_bind


@pytest.mark.asyncio
async def test_entity_bind_skips_search_for_general():
    async with SessionLocal() as session:
        task = Task(idempotency_key=f"e-{uuid.uuid4().hex[:8]}", query="通用内容",
                    content_type="generic", mode="general")
        session.add(task)
        await session.commit()
        await session.refresh(task)
        tid = task.id
    with patch("src.pipeline.nodes.search_image") as mock_search:
        out = await node_entity_bind({"task_id": tid})
    assert out["searched_images"] == 0
    mock_search.assert_not_called()
    async with SessionLocal() as session:
        cnt = (await session.execute(
            select(func.count()).select_from(Asset).where(Asset.task_id == tid))).scalar_one()
        assert cnt == 0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/integration/test_entity_bind_mode.py -v
```
Expected: FAIL（general 模式仍调 `search_image` 并写 official 资产）

- [ ] **Step 3: 改 `node_entity_bind`**

`src/pipeline/nodes.py` 里 `node_entity_bind`：

```python
async def node_entity_bind(input_data: dict) -> dict:
    """搜实景图/实物图，存为 official 素材（compare/single 作参考图；general 跳过）。"""
    import hashlib
    from src.models.tasks import Task
    from src.models.assets import Asset
    from src.gateway.image_search import search_image
    async with SessionLocal() as session:
        task = (await session.execute(
            select(Task).where(Task.id == input_data["task_id"]))).scalar_one()
        query = task.query
        mode = task.mode or "general"
    if mode == "general":
        return {"searched_images": 0}
    images = await search_image(query, count=6)
    async with SessionLocal() as session:
        for i, img in enumerate(images, start=1):
            session.add(Asset(
                task_id=input_data["task_id"], page_index=i,
                subject=query, source_type="official", copyright_status="unknown",
                hash=hashlib.md5(img["image_url"].encode()).hexdigest(),
                image_url=img["image_url"], model_version=img.get("engine", "search"),
                is_illustration=False))
        await session.commit()
    return {"searched_images": len(images)}
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/integration/test_entity_bind_mode.py -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/nodes.py tests/integration/test_entity_bind_mode.py
git commit -m "feat: entity_bind 在 general 模式跳过搜图"
```

---

## Task 6: 生图网关重写为 gpt-image-1.5（文生图 + 图生图）

**Files:**
- Modify: `src/config.py`
- Modify: `src/gateway/image_gen.py`
- Modify: `tests/conftest.py`（`mock_external_calls` 改用 AsyncMock）
- Test: `tests/unit/test_image_gen.py`

**Interfaces:**
- Consumes: `settings.openai_image_base_url`、`settings.openai_image_api_key`、`settings.image_model`、`settings.image_size`
- Produces:
  - `generate_image(prompt: str, size: str = None, reference_image_urls: list[str] | None = None) -> dict`（返回 `{image_url, hash, model_version}`）
  - 内部 `_generate(prompt, size)`（文生图）、`_edit_with_references(prompt, refs, size)`（图生图）

- [ ] **Step 1: 写失败测试**

`tests/unit/test_image_gen.py`：

```python
import pytest
from unittest.mock import AsyncMock, patch
import httpx
from src.gateway import image_gen


@pytest.mark.asyncio
async def test_generate_text_only_routes_to_generate():
    with patch.object(image_gen, "_generate", new=AsyncMock(return_value={"image_url": "u", "hash": "h", "model_version": "gpt-image-1.5"})) as gen, \
         patch.object(image_gen, "_edit_with_references", new=AsyncMock()) as edit:
        await image_gen.generate_image("prompt")
    gen.assert_awaited_once()
    edit.assert_not_called()


@pytest.mark.asyncio
async def test_generate_with_refs_routes_to_edit():
    with patch.object(image_gen, "_edit_with_references", new=AsyncMock(return_value={"image_url": "u", "hash": "h", "model_version": "gpt-image-1.5"})) as edit:
        await image_gen.generate_image("prompt", reference_image_urls=["https://x/a.png"])
    edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_ref_download_failure_falls_back_to_generate():
    with patch.object(image_gen, "_edit_with_references",
                      new=AsyncMock(side_effect=httpx.HTTPStatusError("err", request=None, response=None))) as edit, \
         patch.object(image_gen, "_generate", new=AsyncMock(return_value={"image_url": "u", "hash": "h", "model_version": "gpt-image-1.5"})) as gen:
        await image_gen.generate_image("prompt", reference_image_urls=["https://x/a.png"])
    gen.assert_awaited_once()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/unit/test_image_gen.py -v
```
Expected: FAIL（`image_gen` 模块当前无 `_generate`/`_edit_with_references`）

- [ ] **Step 3: `config.py` 加配置**

`src/config.py` 的 `Settings` 类里，`dashscope_base_url` 之后加：

```python
    # 图片生成（gpt-image-1.5，OpenAI 兼容 Images API，经转发机）
    openai_image_base_url: str = ""      # 转发机地址（占位，联调填）
    openai_image_api_key: str = "sk-xxx" # OpenAI 图生 key
    image_model: str = "gpt-image-1.5"
    image_size: str = "1024x1536"        # 3:4 竖版
```

- [ ] **Step 4: 重写 `src/gateway/image_gen.py`**

```python
import hashlib
import httpx
from src.config import settings

IMAGE_MODEL = settings.image_model
IMAGE_SIZE = settings.image_size


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.openai_image_api_key}"}


async def _download_image_bytes(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


async def generate_image(prompt: str, size: str = None,
                         reference_image_urls: list[str] | None = None) -> dict:
    """调用 gpt-image-1.5 生成一张图；reference_image_urls 非空则图生图。"""
    size = size or IMAGE_SIZE
    if reference_image_urls:
        try:
            return await _edit_with_references(prompt, reference_image_urls, size)
        except httpx.HTTPError:
            # 参考图下载失败 → 降级文生图
            return await _generate(prompt, size)
    return await _generate(prompt, size)


async def _generate(prompt: str, size: str) -> dict:
    """文生图：POST /v1/images/generations"""
    url = f"{settings.openai_image_base_url}/v1/images/generations"
    payload = {"model": IMAGE_MODEL, "prompt": prompt, "size": size, "n": 1}
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(url, json=payload, headers=_headers())
        if resp.status_code >= 400:
            raise RuntimeError(f"image gen failed ({resp.status_code}): {resp.text[:400]}")
        data = resp.json()
    return _result(data["data"][0]["url"])


async def _edit_with_references(prompt: str, reference_image_urls: list[str],
                                size: str) -> dict:
    """图生图：POST /v1/images/edits，参考图 multipart 上传。"""
    url = f"{settings.openai_image_base_url}/v1/images/edits"
    files = []
    for i, ref_url in enumerate(reference_image_urls):
        content = await _download_image_bytes(ref_url)
        files.append(("image[]", (f"ref_{i}.png", content, "image/png")))
    data = {"model": IMAGE_MODEL, "prompt": prompt, "size": size,
            "input_fidelity": "high", "n": "1"}
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(url, data=data, files=files, headers=_headers())
        if resp.status_code >= 400:
            raise RuntimeError(f"image edit failed ({resp.status_code}): {resp.text[:400]}")
        j = resp.json()
    return _result(j["data"][0]["url"])


def _result(image_url: str) -> dict:
    return {"image_url": image_url, "hash": hashlib.md5(image_url.encode()).hexdigest(),
            "model_version": IMAGE_MODEL}
```

> 注意：`image[]` 多参考图字段名以 OpenAI Images API 为准；转发机若用自定义字段名（如 `image`），联调时改 `files.append` 的字段名即可，函数签名与返回结构不变。

- [ ] **Step 5: 更新 `tests/conftest.py` 的 mock**

`mock_external_calls` fixture 里 `generate_image` 的 mock 改为 `AsyncMock`（否则 `await` 一个 dict 会报 `TypeError`）：

```python
from unittest.mock import patch, AsyncMock
...
FAKE_IMAGE = {"hash": "abc123", "image_url": "https://example.com/i.png", "model_version": "gpt-image-1.5"}
...
    with patch("src.gateway.image_gen.generate_image", new=AsyncMock(return_value=FAKE_IMAGE)), \
         patch("src.gateway.web_search.web_search", return_value=FAKE_SEARCH), \
         patch("src.gateway.web_search.deepseek_verify", return_value=FAKE_VERIFY), \
         patch("src.gateway.image_search.search_image", return_value=FAKE_IMAGES):
        yield
```

（同时把 FAKE_IMAGE 的 `model_version` 从 `z-image-turbo` 改为 `gpt-image-1.5`。）

- [ ] **Step 6: 跑测试**

```bash
pytest tests/unit/test_image_gen.py -v
```
Expected: 3 PASS

- [ ] **Step 7: 跑既有生图相关测试确认未回归**

```bash
pytest tests/integration/test_pipeline_image_nodes.py -v
```
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/config.py src/gateway/image_gen.py tests/conftest.py tests/unit/test_image_gen.py
git commit -m "feat: 生图网关切 gpt-image-1.5（文生图 + 图生图）"
```

---

## Task 7: `node_asset_gen` 按 mode 分支（图生图 vs 文生图）

**Files:**
- Modify: `src/pipeline/nodes.py`（`_generate_single_asset`、`node_asset_gen`）
- Test: `tests/integration/test_asset_gen_mode.py`

**Interfaces:**
- Consumes: `generate_image(prompt, size, reference_image_urls)`（Task 6）、`get_image_prompt(mode, page_body)`（Task 2）、`Task.mode`（Task 1）、official 资产（Task 5）
- Produces: `node_asset_gen` 对 general 调 `generate_image` 时 `reference_image_urls=None`；对 compare/single 传 official 资产 URL 列表。

- [ ] **Step 1: 写失败测试**

`tests/integration/test_asset_gen_mode.py`：

```python
import uuid
import pytest
from unittest.mock import AsyncMock, patch
from sqlalchemy import select
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.models.drafts import PageCopy
from src.models.assets import Asset
from src.pipeline.nodes import node_asset_gen

FAKE_IMAGE = {"hash": "abc", "image_url": "https://example.com/i.png", "model_version": "gpt-image-1.5"}


async def _make_task(mode):
    async with SessionLocal() as session:
        task = Task(idempotency_key=f"a-{uuid.uuid4().hex[:8]}", query="q",
                    content_type="x", mode=mode)
        session.add(task)
        await session.commit()
        await session.refresh(task)
        tid = task.id
        for i in range(1, 7):
            session.add(PageCopy(task_id=tid, page_index=i, body=f"第{i}页文案", claim_ids=[]))
        if mode in ("compare", "single"):
            session.add(Asset(task_id=tid, page_index=0, subject="q", source_type="official",
                              copyright_status="unknown", hash="h1",
                              image_url="https://example.com/real.png",
                              model_version="search", is_illustration=False))
        await session.commit()
    return tid


@pytest.mark.asyncio
async def test_asset_gen_general_no_reference():
    tid = await _make_task("general")
    with patch("src.gateway.image_gen.generate_image", new=AsyncMock(return_value=FAKE_IMAGE)) as gen:
        await node_asset_gen({"task_id": tid})
    for call in gen.await_args_list:
        assert call.kwargs.get("reference_image_urls") is None


@pytest.mark.asyncio
async def test_asset_gen_compare_passes_reference():
    tid = await _make_task("compare")
    with patch("src.gateway.image_gen.generate_image", new=AsyncMock(return_value=FAKE_IMAGE)) as gen:
        await node_asset_gen({"task_id": tid})
    refs = [call.kwargs.get("reference_image_urls") for call in gen.await_args_list]
    assert all(r == ["https://example.com/real.png"] for r in refs)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/integration/test_asset_gen_mode.py -v
```
Expected: FAIL（`generate_image` 未按 mode 传参考图）

- [ ] **Step 3: 改 `_generate_single_asset` 与 `node_asset_gen`**

`src/pipeline/nodes.py`：

```python
async def _generate_single_asset(task_id, page_index: int, prompt: str,
                                 reference_image_urls=None) -> dict:
    from src.gateway.image_gen import generate_image
    r = await generate_image(prompt, reference_image_urls=reference_image_urls)
    return {"task_id": task_id, "page_index": page_index, "hash": r["hash"],
            "image_url": r["image_url"],
            "source_type": "ai_generated", "copyright_status": "clear",
            "model_version": r["model_version"], "is_illustration": False}


async def node_asset_gen(input_data: dict) -> dict:
    import asyncio
    from sqlalchemy import select
    from src.models.tasks import Task
    from src.models.assets import Asset
    from src.models.drafts import PageCopy
    from src.gateway.prompt_versions import get_image_prompt
    async with SessionLocal() as session:
        task = (await session.execute(
            select(Task).where(Task.id == input_data["task_id"]))).scalar_one()
        mode = task.mode or "general"
        pages = await session.execute(
            select(PageCopy).where(PageCopy.task_id == input_data["task_id"]))
        page_list = pages.scalars().all()
        reference_urls = []
        if mode in ("compare", "single"):
            refs = await session.execute(
                select(Asset).where(Asset.task_id == input_data["task_id"],
                                    Asset.source_type == "official",
                                    Asset.is_illustration == False))
            reference_urls = [a.image_url for a in refs.scalars() if a.image_url]
    prompts = [get_image_prompt(mode, (p.body or "")[:200]) for p in page_list]
    while len(prompts) < 6:
        prompts.append(get_image_prompt(mode, ""))
    sem = asyncio.Semaphore(2)
    async def _gen(i, prompt):
        async with sem:
            return await _generate_single_asset(input_data["task_id"], i, prompt,
                                                reference_urls)
    results = await asyncio.gather(
        *[_gen(i, prompts[i - 1]) for i in range(1, 7)])
    async with SessionLocal() as session:
        for r in results:
            session.add(Asset(**r))
        await session.commit()
    return {"asset_count": len(results),
            "image_urls": [r.get("image_url") for r in results if r.get("image_url")]}
```

> 注意：`generate_image` 的导入保持在 `_generate_single_asset` 内部（调用时才 `from src.gateway.image_gen import generate_image`），因此测试 patch 目标为 `src.gateway.image_gen.generate_image`（与 conftest 的 autouse mock 同一目标，测试内层 patch 生效并可用 `await_args_list` 断言）。

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/integration/test_asset_gen_mode.py -v
```
Expected: 2 PASS

- [ ] **Step 5: 跑全量测试**

```bash
pytest -q
```
Expected: 全部 PASS（含既有 34 个 + 新增）

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/nodes.py tests/integration/test_asset_gen_mode.py
git commit -m "feat: asset_gen 按 mode 分支（图生图 vs 文生图）"
```

---

## Self-Review 记录

- **Spec 覆盖**：§3 数据模型→Task 1；§4 导入→Task 3；§5 提示词→Task 2；§6.1 draft_gen→Task 4；§6.2 entity_bind→Task 5；§6.3 asset_gen→Task 7；§7 生图网关+§7.3 config→Task 6；§8 开放项（按整条 query 搜图/纯文生图/转发机占位）已在相应任务的实现中体现。
- **类型一致性**：`generate_image(prompt, size=None, reference_image_urls=None)` 签名在 Task 6 定义、Task 7 使用，参数名一致；`get_draft_prompt(mode)`/`get_image_prompt(mode, page_body)` 在 Task 2 定义、Task 4/7 使用，一致。
- **占位符**：无 TBD/TODO；`openai_image_base_url` 占位是明确的开源项（联调填），非计划漏洞。
