from fastapi import FastAPI
from src.api.tasks import router as tasks_router
from src.api.healthcheck import router as healthcheck_router

app = FastAPI(title="query-validation-platform")
app.include_router(tasks_router)
app.include_router(healthcheck_router)
