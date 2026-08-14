FROM python:3.12-slim

WORKDIR /app

# 先复制依赖清单，利用 Docker 层缓存
COPY pyproject.toml ./
COPY src/ ./src/
COPY static/ ./static/
COPY migrations/ ./migrations/

# 安装依赖
RUN pip install --no-cache-dir \
    fastapi uvicorn "sqlalchemy[asyncio]" asyncpg alembic redis litellm \
    openpyxl pydantic pydantic-settings python-multipart httpx

EXPOSE 8000

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
