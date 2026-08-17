from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from src.api.tasks import router as tasks_router
from src.api.healthcheck import router as healthcheck_router
from src.api.entities import router as entities_router
from src.api.review import router as review_router
from src.api.dashboard import router as dashboard_router
from src.api.auth import router as auth_router
from src.api.stream import router as stream_router
from src.api.admin import router as admin_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    from src.stream.scheduler import scheduler
    from src.stream.maintenance import cycle
    from src.stream.progress import progress
    await progress.start()
    await scheduler.start()
    await cycle.start()
    yield
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

# 静态前端（审核工作台 + 看板）
STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return RedirectResponse(url="/login")


@app.get("/login")
async def login():
    return RedirectResponse(url="/static/login.html")


@app.get("/workbench")
async def workbench():
    return RedirectResponse(url="/static/workbench.html")


@app.get("/dashboard")
async def dashboard():
    return RedirectResponse(url="/static/dashboard.html")


@app.get("/import")
async def import_page():
    return RedirectResponse(url="/static/import.html")


@app.get("/progress")
async def progress_page():
    return RedirectResponse(url="/static/progress.html")


@app.get("/stream")
async def stream_page():
    return RedirectResponse(url="/static/stream.html")


@app.get("/sample")
async def sample_page():
    return RedirectResponse(url="/static/sample.html")


@app.get("/admin")
async def admin_page():
    return RedirectResponse(url="/static/admin.html")
