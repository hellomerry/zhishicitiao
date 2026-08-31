"""标杆交付规范读取（迁移 015，借鉴 8003 analyze_benchmark 成果）。

8003 用 81 条真实成功交付案例统计 + LLM 提炼出每 mode 一段【标杆交付规范】
（标题写法/正文结构/口吻/图上文案规律/dos/donts），落 prompt_templates 表
stage='bench_{mode}'（系统级 owner_id IS NULL）。直连路径在 draft_gen /
page_split 提示词中注入该规范，提升文案结构、标题与图上文案质量；
查不到/出错返回 None，不阻塞生成（规范是增益项不是门槛）。
"""
import traceback

from sqlalchemy import text

from src.db.session import SessionLocal


async def get_bench_rule(mode: str) -> str | None:
    """读系统级启用的 bench_{mode} 规范（最新一条）；不存在/出错返回 None。"""
    try:
        async with SessionLocal() as session:
            return (await session.execute(text(
                "SELECT content FROM prompt_templates WHERE stage = :s "
                "AND owner_id IS NULL AND is_active "
                "ORDER BY updated_at DESC LIMIT 1"),
                {"s": f"bench_{mode or 'general'}"})).scalar() or None
    except Exception:
        traceback.print_exc()  # 规范读取失败不阻塞生产
        return None
