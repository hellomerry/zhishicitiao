"""Docker 首次启动时的数据库初始化：建表 + 三个账号（幂等，可重复执行）。"""
import asyncio
import hashlib
import os
import sys
from pathlib import Path

import asyncpg

USERS = [("张三", "A"), ("李四", "B"), ("王五", "C")]


def _dsn() -> str:
    url = os.environ.get("DATABASE_URL", "postgresql+asyncpg://qvp:qvp@postgres:5432/qvp")
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def main() -> None:
    dsn = _dsn()
    conn = None
    for attempt in range(30):
        try:
            conn = await asyncpg.connect(dsn)
            break
        except Exception as exc:  # noqa: BLE001
            print(f"[init] 等待数据库就绪 ({attempt + 1}/30): {exc}")
            await asyncio.sleep(2)
    if conn is None:
        print("[init] 数据库连接失败，退出")
        sys.exit(1)

    try:
        schema = Path("migrations/001_initial_schema.sql").read_text()
        await conn.execute(schema)
        print("[init] 21 张表已就绪")

        pw = hashlib.sha256("12345678".encode()).hexdigest()
        for name, role in USERS:
            exists = await conn.fetchval("SELECT id FROM users WHERE name = $1", name)
            if exists:
                await conn.execute(
                    "UPDATE users SET password_hash = $1, role = $2 WHERE name = $3",
                    pw, role, name,
                )
            else:
                await conn.execute(
                    "INSERT INTO users (name, role, password_hash) VALUES ($1, $2, $3)",
                    name, role, pw,
                )
        print("[init] 账号就绪（张三/李四/王五，密码 12345678）")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
