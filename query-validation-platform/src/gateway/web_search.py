import litellm
from src.config import settings

# DeepSeek 内置联网搜索（Responses API web_search 工具），替代豆包搜索做证据包
SEARCH_MODEL = "deepseek/deepseek-v4-pro"


async def web_search(query: str) -> str:
    """DeepSeek 服务端联网搜索，返回搜索总结文本。"""
    r = await litellm.aresponses(
        model=SEARCH_MODEL,
        input=query,
        tools=[{"type": "web_search"}],
        api_key=settings.deepseek_api_key)
    if hasattr(r, "output_text"):
        return r.output_text or ""
    return str(r)
