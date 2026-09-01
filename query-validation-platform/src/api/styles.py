"""风格关键词库 API：风格 → 关键词/描述词 的可训练映射（"知识训练"落地，2026-08-28 移植）。

- 用户在设置页维护或 CSV 批量导入训练数据（style_name,keywords,description）
- asset_gen 生图前按 query+正文自动匹配最优风格，描述词替换生图模板中的
  固定风格句；库为空则回退内置 8 风格（src/services/style_pick.py）

权限模型（迁移 012，与 prompt_templates 同口径）：
- owner_id IS NULL = admin 公共库；非空 = 个人库。GET 返回「我的 + 公共」。
- 普通用户只能写/删自己的条目；admin 额外可写/删公共条目（public=true）。
- 越权写/删 → 403；条目不存在 → 404。actor 缺失/未知 → 401。
"""
import csv
import io
import uuid

import httpx

from fastapi import APIRouter, Form, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import select, text

from src.db.session import SessionLocal
from src.models.styles import StyleKeyword
from src.services.activity import log_action

router = APIRouter()


async def _actor(session, name: str):
    """按用户名查 (id, role)；缺失/查不到 → 401（与 prompts.py 同口径）。"""
    if not name:
        raise HTTPException(status_code=401, detail="缺少 actor 参数")
    row = (await session.execute(
        text("SELECT id, role FROM users WHERE name = :n AND active"),
        {"n": name})).first()
    if not row:
        raise HTTPException(status_code=401, detail=f"用户不存在: {name}")
    return row[0], row[1]


def _row(r: StyleKeyword, owner_name: str = None) -> dict:
    return {
        "id": str(r.id), "style_name": r.style_name, "keywords": r.keywords,
        "description": r.description, "enabled": r.enabled,
        "owner_id": str(r.owner_id) if r.owner_id else None,
        "owner_name": owner_name,           # NULL owner → None（前端显示「公共」）
        "scope": "public" if r.owner_id is None else "mine",
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/api/styles")
async def list_styles(actor: str = ""):
    """风格关键词库：我的条目 + 公共条目（生成时的自动匹配选项来源）。"""
    async with SessionLocal() as session:
        uid, _ = await _actor(session, actor)
        rows = list((await session.execute(
            select(StyleKeyword)
            .where((StyleKeyword.owner_id == uid) | (StyleKeyword.owner_id.is_(None)))
            .order_by(StyleKeyword.created_at))).scalars().all())
        names = dict((await session.execute(
            text("SELECT id, name FROM users"))).all())
    return {"items": [_row(r, names.get(r.owner_id)) for r in rows]}


class StyleIn(BaseModel):
    style_name: str
    keywords: str = ""
    description: str = ""
    enabled: bool = True
    public: bool = False      # 写入公共库（仅 admin；默认写个人库）


def _scope_query(uid, public: bool):
    """同一 owner 作用域内的同名查询条件（公共=NULL owner）。"""
    if public:
        return StyleKeyword.owner_id.is_(None)
    return StyleKeyword.owner_id == uid


@router.post("/api/styles")
async def upsert_style(payload: StyleIn, actor: str = ""):
    """新增/更新风格条目（同 owner 内同名覆盖更新）。public=true 仅 admin。"""
    name = payload.style_name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="风格名不能为空")
    async with SessionLocal() as session:
        uid, role = await _actor(session, actor)
        if payload.public and role != "admin":
            raise HTTPException(status_code=403, detail="仅管理员可维护公共风格库")
        row = (await session.execute(
            select(StyleKeyword).where(_scope_query(uid, payload.public),
                                       StyleKeyword.style_name == name))
        ).scalars().first()
        if row:
            row.keywords = payload.keywords.strip()
            row.description = payload.description.strip()
            row.enabled = payload.enabled
        else:
            session.add(StyleKeyword(
                owner_id=None if payload.public else uid,
                style_name=name, keywords=payload.keywords.strip(),
                description=payload.description.strip(),
                enabled=payload.enabled))
        await session.commit()
    await log_action(actor, "style_kb",
                     f"保存{'公共' if payload.public else ''}风格「{name}」")
    return {"ok": True, "style_name": name}


@router.delete("/api/styles/default")
async def clear_default_style(actor: str = ""):
    """取消个人默认风格（恢复自动判定）。
    注意：必须声明在 /api/styles/{style_id} 之前，否则 "default" 被当 id 解析。"""
    async with SessionLocal() as session:
        uid, _ = await _actor(session, actor)
        await session.execute(
            text("UPDATE users SET default_style = NULL WHERE id = :u"),
            {"u": uid})
        await session.commit()
    await log_action(actor, "style_kb", "取消默认风格")
    return {"ok": True, "default_style": None}


@router.delete("/api/styles/{style_id}")
async def delete_style(style_id: str, actor: str = ""):
    try:
        sid = uuid.UUID(style_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid id")
    async with SessionLocal() as session:
        uid, role = await _actor(session, actor)
        row = (await session.execute(
            select(StyleKeyword).where(StyleKeyword.id == sid))).scalars().first()
        if not row:
            raise HTTPException(status_code=404, detail="not found")
        if role != "admin":
            if row.owner_id is None:
                raise HTTPException(status_code=403, detail="仅管理员可删除公共风格")
            if row.owner_id != uid:
                raise HTTPException(status_code=403, detail="只能删除自己的风格")
        name = row.style_name
        await session.delete(row)
        await session.commit()
    await log_action(actor, "style_kb", f"删除风格「{name}」")
    return {"ok": True}


@router.post("/api/styles/import")
async def import_styles(file: UploadFile = File(...), actor: str = Form(""),
                        public: bool = Form(False)):
    """CSV 批量导入训练数据：列 style_name,keywords,description（同 owner 内同名
    覆盖）。默认导入操作者个人库；admin 传 public=true 导入公共库。"""
    content = await file.read()
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    imported, errors = 0, []
    async with SessionLocal() as session:
        uid, role = await _actor(session, actor)
        if public and role != "admin":
            raise HTTPException(status_code=403, detail="仅管理员可导入公共风格库")
        for row in reader:
            try:
                name = (row.get("style_name") or "").strip()
                if not name:
                    raise ValueError("style_name 为空")
                exist = (await session.execute(
                    select(StyleKeyword).where(_scope_query(uid, public),
                                               StyleKeyword.style_name == name))
                ).scalars().first()
                if exist:
                    exist.keywords = (row.get("keywords") or "").strip()
                    exist.description = (row.get("description") or "").strip()
                else:
                    session.add(StyleKeyword(
                        owner_id=None if public else uid,
                        style_name=name,
                        keywords=(row.get("keywords") or "").strip(),
                        description=(row.get("description") or "").strip()))
                imported += 1
            except Exception as e:  # noqa: BLE001
                errors.append({"row": dict(row), "error": str(e)})
        await session.commit()
    if imported:
        await log_action(actor, "style_kb",
                         f"批量导入{'公共' if public else ''}风格关键词 "
                         f"{imported} 条（文件 {file.filename}）")
    return {"imported": imported, "errors": errors}


async def style_library_text() -> str | None:
    """风格判定注入文本：启用的公共条目（名称+描述词）；库空返回 None（用内置）。"""
    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(StyleKeyword).where(StyleKeyword.enabled,
                                       StyleKeyword.owner_id.is_(None))
            .order_by(StyleKeyword.created_at))).scalars().all())
    if not rows:
        return None
    return "\n".join(f"- {r.style_name}：{r.description}" for r in rows)


@router.get("/api/styles/template")
async def styles_template():
    """CSV 导入模板下载。"""
    from fastapi.responses import Response
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["style_name", "keywords", "description"])
    w.writerow(["科技蓝调", "手机,数码,芯片,参数", "深蓝主色调配科技光感、几何线条、数据可视化元素"])
    w.writerow(["暖木家居", "家具,装修,木纹,客厅", "暖木色系、自然光、居家生活场景"])
    return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             "attachment; filename=style_keywords_template.csv"})


@router.get("/api/styles/lookup")
async def lookup_style(style_name: str, actor: str = ""):
    """按风格名反查描述词（个人→公共→内置），给「存为我的风格」预填表单用。"""
    from src.services.style_pick import style_desc_for
    async with SessionLocal() as session:
        uid, _ = await _actor(session, actor)
    return {"style_name": style_name,
            "description": await style_desc_for(style_name, uid) or ""}


@router.get("/api/styles/stats")
async def style_stats(actor: str):
    """我的风格偏好统计（2026-08-28）：按历史任务的 gen_image_style 聚合——
    任务数、审核通过数/率（tasks.status：approved=通过，rejected=驳回，
    通过率=通过/(通过+驳回)，无已审任务为 None）、定点重生成次数（被替换的
    AI 配图历史版本数，is_active=false，迁移 009 版本机制）。附带当前默认风格。"""
    async with SessionLocal() as session:
        uid, _ = await _actor(session, actor)
        rows = (await session.execute(text(
            "SELECT gen_image_style AS style, count(*) AS total,"
            " count(*) FILTER (WHERE status = 'approved') AS approved,"
            " count(*) FILTER (WHERE status = 'rejected') AS rejected"
            " FROM tasks"
            " WHERE created_by = :u AND gen_image_style IS NOT NULL"
            "   AND gen_image_style <> ''"
            " GROUP BY gen_image_style ORDER BY total DESC"),
            {"u": uid})).all()
        regen = dict((await session.execute(text(
            "SELECT t.gen_image_style, count(*)"
            " FROM assets a JOIN tasks t ON t.id = a.task_id"
            " WHERE t.created_by = :u AND a.source_type = 'ai_generated'"
            "   AND a.is_active = false AND t.gen_image_style IS NOT NULL"
            "   AND t.gen_image_style <> ''"
            " GROUP BY t.gen_image_style"),
            {"u": uid})).all())
        default = (await session.execute(
            text("SELECT default_style FROM users WHERE id = :u"),
            {"u": uid})).scalar()
    items = []
    for style, total, approved, rejected in rows:
        reviewed = approved + rejected
        items.append({
            "style_name": style, "total": total, "approved": approved,
            "rejected": rejected,
            "approval_rate": round(approved / reviewed, 4) if reviewed else None,
            "regen_count": int(regen.get(style, 0)),
        })
    return {"items": items, "default_style": default}


class DefaultStyleIn(BaseModel):
    actor: str
    style_name: str


@router.post("/api/styles/default")
async def set_default_style(payload: DefaultStyleIn):
    """钉选个人默认风格（users.default_style）：style_pick 最优先直接使用，
    跳过 LLM 判定。"""
    name = payload.style_name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="风格名不能为空")
    async with SessionLocal() as session:
        uid, _ = await _actor(session, payload.actor)
        await session.execute(
            text("UPDATE users SET default_style = :s WHERE id = :u"),
            {"s": name, "u": uid})
        await session.commit()
    await log_action(payload.actor, "style_kb", f"设默认风格「{name}」")
    return {"ok": True, "default_style": name}


# ===== 风格模板开局 + 样例学风格（2026-09-01，「不同用户不同风格」方案）=====
# 设计原则：风格差异来自用户显式选择（即时生效），偏好学习只做细节微调、
# 永不替换用户钉选的风格（学习降级见 style_pick.py）


@router.get("/api/styles/templates")
async def list_templates():
    """内置风格模板列表（onboarding 选风格样卡数据源）：名称/关键词/描述词。"""
    from src.services.style_pick import IMAGE_STYLE_LIBRARY
    return {"items": [{"style_name": n, "keywords": k, "description": d}
                      for n, d, k in IMAGE_STYLE_LIBRARY]}


@router.get("/api/styles/onboarding_state")
async def onboarding_state(actor: str = ""):
    """是否需要风格开局引导：个人库为空且未钉默认风格 → true（新用户首次进入）。"""
    async with SessionLocal() as session:
        uid, _ = await _actor(session, actor)
        mine = (await session.execute(
            select(StyleKeyword).where(StyleKeyword.owner_id == uid))).scalars().all()
        default = (await session.execute(
            text("SELECT default_style FROM users WHERE id = :u"),
            {"u": uid})).scalar()
    return {"needs_onboarding": not mine and not default,
            "default_style": default, "mine_count": len(mine)}


class CloneTemplatesIn(BaseModel):
    actor: str
    style_names: list[str]          # 要克隆的内置模板名（1-3 个）
    pin: str = ""                   # 同时钉为默认的风格名（须含在 style_names 内）


@router.post("/api/styles/clone_templates")
async def clone_templates(payload: CloneTemplatesIn):
    """把内置风格模板克隆到个人库（同名已存在则跳过、保留用户自己改过的版本），
    可顺带钉选默认——新用户 30 秒完成风格开局，无需等待偏好学习积累。"""
    from src.services.style_pick import IMAGE_STYLE_LIBRARY
    builtin = {n: (d, k) for n, d, k in IMAGE_STYLE_LIBRARY}
    names = [n.strip() for n in payload.style_names if n.strip() in builtin]
    if not names:
        raise HTTPException(status_code=422, detail="没有可克隆的内置模板名")
    if payload.pin and payload.pin not in names:
        raise HTTPException(status_code=422, detail="pin 必须含在 style_names 内")
    cloned, skipped = [], []
    async with SessionLocal() as session:
        uid, _ = await _actor(session, payload.actor)
        for name in names[:3]:
            exist = (await session.execute(
                select(StyleKeyword).where(StyleKeyword.owner_id == uid,
                                           StyleKeyword.style_name == name))
            ).scalars().first()
            if exist:
                skipped.append(name)
                continue
            desc, kws = builtin[name]
            session.add(StyleKeyword(owner_id=uid, style_name=name,
                                     keywords=kws, description=desc))
            cloned.append(name)
        if payload.pin:
            await session.execute(
                text("UPDATE users SET default_style = :s WHERE id = :u"),
                {"s": payload.pin, "u": uid})
        await session.commit()
    await log_action(payload.actor, "style_kb",
                     f"克隆风格模板 {cloned}" +
                     (f"，钉选默认「{payload.pin}」" if payload.pin else ""))
    return {"ok": True, "cloned": cloned, "skipped": skipped,
            "default_style": payload.pin or None}


_LEARN_PROMPT = """你是小红书图文风格分析师。分析这张图片的视觉风格，只输出 JSON：
{"style_name": "≤8字的风格名",
 "keywords": "适用主题关键词，逗号分隔，5-8个",
 "description": "视觉风格描述词（40-80字）：底色/主体质感/光影/标题排版与配色/文字载体形式/装饰元素/留白比例，要能直接用作 AI 生图提示词的风格描述部分"}
不要输出任何其他内容。"""


@router.post("/api/styles/learn")
async def learn_from_images(actor: str = Form(""),
                            files: list[UploadFile] = File(...)):
    """样例学风格：上传 1-5 张满意图，qwen-vl 提炼风格草稿（名称/关键词/描述词）
    返回给用户编辑确认后再经 POST /api/styles 保存——把离线的「样例训练」变成
    每个用户自助的在线能力。VL 调用失败 502，不落库。"""
    import base64
    import json as _json
    from src.config import settings
    async with SessionLocal() as session:
        await _actor(session, actor)
    files = files[:5]
    if not files:
        raise HTTPException(status_code=422, detail="至少上传 1 张图片")
    content = []
    for f in files:
        data = await f.read()
        if len(data) > 15 * 1024 * 1024:
            raise HTTPException(status_code=422, detail=f"{f.filename} 超过 15MB")
        mime = f.content_type if (f.content_type or "").startswith("image/") \
            else "image/png"
        content.append({"type": "image_url", "image_url": {
            "url": f"data:{mime};base64," + base64.b64encode(data).decode()}})
    if len(files) > 1:
        content.append({"type": "text", "text":
                        f"以上 {len(files)} 张图是同一套目标风格的样例。"})
    content.append({"type": "text", "text": _LEARN_PROMPT})
    payload = {"model": settings.vl_review_model,
               "messages": [{"role": "user", "content": content}],
               "max_tokens": 500}
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{settings.ocr_base_url}/chat/completions",
                headers={"Authorization":
                         f"Bearer {settings.dashscope_api_key}"},
                json=payload)
        if resp.status_code != 200:
            raise RuntimeError(f"vl {resp.status_code}")
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        raw = raw.strip("`").lstrip("json").strip()
        j = _json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"风格分析失败: {e}")
    await log_action(actor, "style_kb",
                     f"从 {len(files)} 张样例图提炼风格草稿")
    return {"style_name": str(j.get("style_name", ""))[:20],
            "keywords": str(j.get("keywords", ""))[:200],
            "description": str(j.get("description", ""))[:300]}
