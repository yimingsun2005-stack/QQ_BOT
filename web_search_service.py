from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import aiohttp


SEARCH_ENDPOINT = "https://api.deepseek.com/anthropic/v1/messages"
CHINA_TIMEZONE = timezone(timedelta(hours=8))
_CURRENT_QUERY = re.compile(
    r"今天|今日|最新|实时|当前|现在|近期|近日|最近|本周|本月|今年|新闻|"
    r"\btoday\b|\blatest\b|\bcurrent\b|\brecent\b|\bnews\b",
    re.IGNORECASE,
)
_YEAR = re.compile(r"(?<!\d)20\d{2}(?!\d)")


def current_china_date() -> date:
    return datetime.now(CHINA_TIMEZONE).date()


def dated_search_query(query: str, today: date) -> str:
    years = set(_YEAR.findall(query))
    if not _CURRENT_QUERY.search(query) or (years and str(today.year) not in years):
        return query
    current_date = f"{today.year}年{today.month}月{today.day}日"
    if current_date in query or today.isoformat() in query:
        return query
    return f"{query} {current_date}"


class WebSearchError(RuntimeError):
    """The server-side search could not produce verifiable results."""


class DeepSeekWebSearch:
    """Use DeepSeek's Anthropic-compatible server-side web search tool."""

    def __init__(self, api_key: str, model: str, timeout_seconds: float) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    async def search(self, query: str) -> list[dict[str, str]]:
        query = query.strip()
        if not query or len(query) > 200:
            raise WebSearchError("搜索词不能为空或超过 200 字")

        today = current_china_date()
        search_query = dated_search_query(query, today)
        payload = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Current date in China is {today.isoformat()}. "
                                f"Perform a web search for: {search_query}. "
                                "For current events, check source dates and prefer recent pages. "
                                "If today's sources are unavailable, do not claim the results are from today."
                            ),
                        }
                    ],
                }
            ],
            "tools": [
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 2}
            ],
        }
        headers = {
            "x-api-key": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    SEARCH_ENDPOINT,
                    headers=headers,
                    json=payload,
                    allow_redirects=False,
                ) as response:
                    if response.status < 200 or response.status >= 300:
                        raise WebSearchError(f"DeepSeek 搜索接口返回 HTTP {response.status}")
                    data = await response.json(content_type=None)
        except WebSearchError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise WebSearchError("无法连接 DeepSeek 搜索接口") from exc
        except ValueError as exc:
            raise WebSearchError("DeepSeek 搜索接口返回的不是有效 JSON") from exc

        return self._extract_results(data)

    @staticmethod
    def _extract_results(data: Any) -> list[dict[str, str]]:
        if not isinstance(data, dict) or not isinstance(data.get("content"), list):
            raise WebSearchError("DeepSeek 搜索响应格式无效")
        if data.get("stop_reason") == "pause_turn":
            raise WebSearchError("DeepSeek 搜索尚未完成")

        blocks = data["content"]
        result_blocks = [
            block
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "web_search_tool_result"
        ]
        if not result_blocks:
            raise WebSearchError("DeepSeek 没有执行联网搜索")

        excerpts: dict[str, str] = {}
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            for citation in block.get("citations") or []:
                if not isinstance(citation, dict):
                    continue
                url = citation.get("url")
                excerpt = citation.get("cited_text")
                if isinstance(url, str) and isinstance(excerpt, str) and url not in excerpts:
                    excerpts[url] = excerpt[:500]

        results: list[dict[str, str]] = []
        seen: set[str] = set()
        for block in result_blocks:
            for item in block.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "web_search_tool_result_error":
                    raise WebSearchError("DeepSeek 联网搜索执行失败")
                if not isinstance(item, dict) or item.get("type") != "web_search_result":
                    continue
                url = item.get("url")
                if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                    continue
                if url in seen:
                    continue
                seen.add(url)
                result = {"url": url[:1000]}
                if isinstance(item.get("title"), str):
                    result["title"] = item["title"][:200]
                if isinstance(item.get("page_age"), str):
                    result["page_age"] = item["page_age"][:80]
                if url in excerpts:
                    result["snippet"] = excerpts[url]
                results.append(result)
                if len(results) == 5:
                    return results
        return results
