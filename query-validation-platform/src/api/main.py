import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from src.api.tasks import router as tasks_router
from src.api.healthcheck import router as healthcheck_router
from src.api.entities import router as entities_router
from src.api.review import router as review_router
from src.api.dashboard import router as dashboard_router
from src.api.auth import router as auth_router
from src.api.stream import router as stream_router
from src.api.admin import router as admin_router
from src.api.prompts import router as prompts_router
from src.api.activity import router as activity_router
from src.api.meta import router as meta_router
from src.api.trash import router as trash_router
from src.api.styles import router as styles_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    from src.stream.scheduler import scheduler
    from src.stream.maintenance import cycle
    from src.stream.progress import progress
    from src.review.heartbeat import heartbeat_loop
    await progress.start()
    await scheduler.start()
    await cycle.start()
    heartbeat_task = asyncio.create_task(heartbeat_loop())
    yield
    heartbeat_task.cancel()
    with suppress(asyncio.CancelledError):
        await heartbeat_task
    await cycle.stop()
    await scheduler.stop()
    await progress.stop()


app = FastAPI(title="query-validation-platform", lifespan=lifespan)
app.include_router(tasks_router)
app.include_router(healthcheck_router)
app.include_router(entities_router)
app.include_router(review_router)
app.include_router(dashboard_router)
app.include_router(auth_router)
app.include_router(stream_router)
app.include_router(admin_router)
app.include_router(prompts_router)
app.include_router(activity_router)
app.include_router(meta_router)
app.include_router(trash_router)
app.include_router(styles_router)

# 静态前端（审核工作台 + 看板）
STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# SPA 入口（static/index.html + hash 路由）
# no-cache：强制浏览器每次重验证（etag 兜底 304），保证 ?v= 版本号 bump 立即生效，
# 避免前端更新后用户端仍加载旧 JS（2026-08-26 风险原因中文化被旧缓存挡住的教训）
@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"),
                        headers={"Cache-Control": "no-cache"})


# 旧页面路径 → SPA hash 路由（兼容旧链接/书签）
_LEGACY_ROUTES = {
    "/login": "/#/login",
    "/workbench": "/#/review",
    "/dashboard": "/#/",
    "/import": "/#/import",
    "/progress": "/#/",
    "/stream": "/#/tasks",
    "/sample": "/#/sample",
    "/admin": "/#/admin",
}

for _path, _target in _LEGACY_ROUTES.items():
    app.add_api_route(_path, lambda t=_target: RedirectResponse(url=t), methods=["GET"])


# 前端错误信标（2026-09-02）：app.js 捕获 window.onerror / unhandledrejection /
# Vue errorHandler 后 POST 到这里，打进容器日志——排查「只在用户浏览器出现」的
# 前端报错（如 admin 任务详情打不开）时不必让用户截 Console。
@app.post("/api/client_errors")
async def client_errors(payload: dict):
    import logging
    logging.getLogger("uvicorn.error").warning(
        "[client_error] user=%s kind=%s msg=%s stack=%s href=%s",
        payload.get("user"), payload.get("kind"),
        str(payload.get("msg"))[:500], str(payload.get("stack"))[:800],
        payload.get("href"))
    return {"ok": True}
