# 产能验证平台

基于 spec `docs/superpowers/specs/2026-08-13-产能验证平台-design.md` 实施。

## 启动

```bash
docker compose up -d
uv sync
uv run uvicorn src.api.main:app --reload
```

## 测试

```bash
uv run pytest
```
