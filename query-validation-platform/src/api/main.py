from fastapi import FastAPI
from src.api.tasks import router as tasks_router
from src.api.healthcheck import router as healthcheck_router
from src.api.entities import router as entities_router
from src.api.review import router as review_router
from src.api.dashboard import router as dashboard_router

app = FastAPI(title="query-validation-platform")
app.include_router(tasks_router)
app.include_router(healthcheck_router)
app.include_router(entities_router)
app.include_router(review_router)
app.include_router(dashboard_router)
