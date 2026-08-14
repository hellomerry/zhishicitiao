import httpx
import litellm
from src.config import settings


async def web_search(query: str, count: int = 6) -> list:
    """网页搜索（证据包），返回结构化结果 list[dict]（title/url/summary）。

    预留 provider 切换：doubao（结构化来源，默认）/ deepseek（联网总结）。
    """
    provider = settings.web_search_provider
    if provider == "doubao":
        return await _search_doubao(query, count)
    if provider == "deepseek":
        return await _search_deepseek(query)
    raise ValueError(f"unknown web search provider: {provider}")


async def _search_doubao(query: str, count: int) -> list:
    url = "https://open.feedcoopapi.com/search_api/web_search"
    body = {
        "Query": query, "SearchType": "web", "Count": count,
        "Filter": {"NeedContent": False, "NeedUrl": True}, "NeedSummary": True,
    }
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {settings.doubao_search_key}"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    results = []
    for item in data.get("Result", {}).get("WebResults", []):
        results.append({
            "title": item.get("Title", ""),
            "url": item.get("Url", ""),
            "summary": item.get("Summary", item.get("Snippet", "")),
        })
    return results


async def _search_deepseek(query: str) -> list:
    r = await litellm.aresponses(
        model="deepseek/deepseek-v4-pro", input=query,
        tools=[{"type": "web_search"}], api_key=settings.deepseek_api_key)
    text = r.output_text if hasattr(r, "output_text") else str(r)
    return [{"title": "deepseek-web-search", "url": "", "summary": text}]
