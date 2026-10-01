import unittest
from datetime import date

from web_search_service import DeepSeekWebSearch, WebSearchError, dated_search_query


class DeepSeekWebSearchTests(unittest.TestCase):
    def test_extracts_only_results_from_executed_search(self) -> None:
        data = {
            "stop_reason": "end_turn",
            "content": [
                {"type": "text", "text": "回答", "citations": [{"url": "https://example.com/news", "cited_text": "可核实的摘要"}]},
                {"type": "web_search_tool_result", "content": [
                    {"type": "web_search_result", "url": "https://example.com/news", "title": "新闻", "page_age": "September 28, 2026"},
                    {"type": "web_search_result", "url": "https://example.com/news", "title": "重复"},
                ]},
            ],
        }
        self.assertEqual(
            DeepSeekWebSearch._extract_results(data),
            [{"url": "https://example.com/news", "title": "新闻", "page_age": "September 28, 2026", "snippet": "可核实的摘要"}],
        )

    def test_current_search_query_uses_today_without_changing_historical_queries(self) -> None:
        today = date(2026, 9, 28)
        self.assertEqual(
            dated_search_query("今天科技新闻", today),
            "今天科技新闻 2026年9月28日",
        )
        self.assertEqual(
            dated_search_query("2025年科技新闻", today),
            "2025年科技新闻",
        )
        self.assertEqual(
            dated_search_query("2026年9月28日最新新闻", today),
            "2026年9月28日最新新闻",
        )
        self.assertEqual(dated_search_query("光合作用原理", today), "光合作用原理")

    def test_prose_without_search_result_is_not_treated_as_search(self) -> None:
        with self.assertRaisesRegex(WebSearchError, "没有执行联网搜索"):
            DeepSeekWebSearch._extract_results({"content": [{"type": "text", "text": "我搜索过了"}]})

    def test_paused_search_is_not_treated_as_complete(self) -> None:
        with self.assertRaisesRegex(WebSearchError, "尚未完成"):
            DeepSeekWebSearch._extract_results({"stop_reason": "pause_turn", "content": []})

    def test_search_tool_error_is_not_treated_as_empty_results(self) -> None:
        with self.assertRaisesRegex(WebSearchError, "执行失败"):
            DeepSeekWebSearch._extract_results({
                "content": [{"type": "web_search_tool_result", "content": [
                    {"type": "web_search_tool_result_error", "error_code": "unavailable"}
                ]}]
            })
