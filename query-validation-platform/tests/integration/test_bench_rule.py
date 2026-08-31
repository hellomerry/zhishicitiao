"""标杆交付规范读取（src/services/bench_rule.py）：

- 库中无 bench_ 条目 → None（不阻塞生成）
- 系统级启用条目 → 返回内容；停用条目不返回
- mode 缺省按 general 查
"""
import pytest
from sqlalchemy import text

from src.db.session import SessionLocal
from src.services.bench_rule import get_bench_rule


async def _add_bench(stage, content="规范内容", is_active=True, owner_null=True):
    async with SessionLocal() as session:
        await session.execute(text(
            "INSERT INTO prompt_templates (stage, mode, owner_id, name, content,"
            " is_active) VALUES (:s, NULL, :o, '测试规范', :c, :a)"),
            {"s": stage, "o": None if owner_null else _ADMIN_ID,
             "c": content, "a": is_active})
        await session.commit()


# 非系统级条目需要一个属主用户
_ADMIN_ID = None


@pytest.fixture(autouse=True)
async def _admin():
    global _ADMIN_ID
    async with SessionLocal() as session:
        _ADMIN_ID = (await session.execute(text(
            "INSERT INTO users (name, role) VALUES ('bench-admin', 'admin')"
            " RETURNING id"))).scalar()
        await session.commit()


@pytest.mark.asyncio
async def test_empty_returns_none():
    assert await get_bench_rule("general") is None


@pytest.mark.asyncio
async def test_active_system_row_returned():
    await _add_bench("bench_compare", "对比规范正文")
    assert await get_bench_rule("compare") == "对比规范正文"


@pytest.mark.asyncio
async def test_inactive_not_returned():
    await _add_bench("bench_single", "单品规范", is_active=False)
    assert await get_bench_rule("single") is None


@pytest.mark.asyncio
async def test_user_owned_not_returned():
    # 非系统级（owner_id 非空）的同名条目不注入
    await _add_bench("bench_general", "个人规范", owner_null=False)
    assert await get_bench_rule("general") is None


@pytest.mark.asyncio
async def test_none_mode_defaults_to_general():
    await _add_bench("bench_general", "通用规范")
    assert await get_bench_rule(None) == "通用规范"
