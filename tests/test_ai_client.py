import unittest
import json
from datetime import date
from typing import Any
from unittest.mock import patch

from ai_client import AIConfig, AIReplyService, AIServiceError, WebSearchConfig
from group_context import ContextMessage


class FakeAIReplyService(AIReplyService):
    def __init__(self, replies: list[str], *, web_search_enabled: bool = True) -> None:
        super().__init__(
            AIConfig(api_key="test-key", max_reply_chars=10),
            WebSearchConfig(enabled=web_search_enabled),
        )
        self.replies = replies
        self.requests: list[tuple[list[dict[str, Any]], bool]] = []

    async def _request_response(
        self,
        messages: list[dict[str, Any]],
        *,
        web_search_enabled: bool,
    ) -> str:
        self.requests.append((messages, web_search_enabled))
        return self.replies.pop(0)


class AIReplyServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_supplied_short_term_context_is_sent(self) -> None:
        service = FakeAIReplyService(["第一次回答", "第二次回答"])
        await service.generate_reply("100", "问题一")
        await service.generate_reply("100", "问题二")
        second_request, search_enabled = service.requests[1]
        self.assertTrue(search_enabled)
        self.assertEqual(len(second_request), 1)
        self.assertEqual(second_request[0]["content"], "问题二")

    async def test_context_is_formatted_and_image_is_included(self) -> None:
        service = FakeAIReplyService(["好"])
        context = [
            ContextMessage(1, "小明", "这是什么", "data:image/png;base64,aGk="),
            ContextMessage(2, "Bot", "像是猫", is_bot=True),
        ]
        await service.generate_reply("100", "像是猫", context)
        messages = service.requests[0][0]
        self.assertEqual([entry["role"] for entry in messages], ["user", "assistant", "user"])
        self.assertEqual(messages[0]["content"][0]["text"], "[历史消息｜发送者：小明] 这是什么")
        self.assertEqual(messages[0]["content"][1]["type"], "input_image")
        self.assertIn("[当前消息", messages[-1]["content"][0]["text"])

    async def test_only_last_member_is_current_and_target_metadata_is_preserved(self) -> None:
        service = FakeAIReplyService(["好"])
        context = [
            ContextMessage(1, "成员#aaaa（甲）", "旧话题"),
            ContextMessage(2, "成员#bbbb（乙）", "问Bot", addressing="明确@Bot；同时@成员#aaaa"),
        ]
        await service.generate_reply("group", "问Bot", context)
        messages = service.requests[0][0]
        self.assertEqual(messages[0]["content"][0]["text"], "[历史消息｜发送者：成员#aaaa（甲）] 旧话题")
        self.assertEqual(
            messages[1]["content"][0]["text"],
            "[当前消息｜发送者：成员#bbbb（乙）；指向：明确@Bot；同时@成员#aaaa] 问Bot",
        )

    async def test_generated_reply_drops_leading_bot_label(self) -> None:
        self.assertEqual(
            await FakeAIReplyService(["[Bot] 好的"]).generate_reply("100", "问题"),
            "好的",
        )
        self.assertEqual(
            await FakeAIReplyService(["【bot】 收到"]).generate_reply("100", "问题"),
            "收到",
        )

    async def test_image_only_context_adds_generic_prompt_once(self) -> None:
        service = FakeAIReplyService(["看起来像只猫"])
        context = [ContextMessage(1, "小明", "", "data:image/png;base64,aGk=")]
        await service.generate_reply("100", "", context)
        messages = service.requests[0][0]
        self.assertEqual(len(messages), 1)
        self.assertIn("[当前消息｜发送者：小明] 发来一张图片", messages[0]["content"][0]["text"])
        self.assertEqual(messages[0]["content"][1]["type"], "input_image")

    async def test_empty_response_is_rejected(self) -> None:
        with self.assertRaises(AIServiceError):
            await FakeAIReplyService(["   "]).generate_reply("100", "问题")

    async def test_reply_is_truncated(self) -> None:
        self.assertEqual(
            await FakeAIReplyService(["1234567890EXTRA"]).generate_reply("100", "问题"),
            "1234567890",
        )

    async def test_source_section_is_removed_from_reply(self) -> None:
        service = FakeAIReplyService(["直接回答\n\n来源：示例 https://example.com"])
        self.assertEqual(await service.generate_reply("100", "问题"), "直接回答")

    async def test_dsml_tool_call_leak_is_retried_without_search(self) -> None:
        service = FakeAIReplyService(
            ['<||DSML||calls>\n<||DSML||invoke name="web_search">', "正常回答"]
        )
        self.assertEqual(await service.generate_reply("100", "今天是几号"), "正常回答")
        self.assertEqual([enabled for _, enabled in service.requests], [True, False])

    async def test_toolcall_leak_from_screenshot_is_retried(self) -> None:
        service = FakeAIReplyService(
            [
                '@情绪 <toolcall>\n{"name": "web_search", '
                '"arguments": {"query": "游戏评价"}}\n</toolcall>',
                "可以从剧情和玩法两方面评价。",
            ]
        )
        self.assertEqual(
            await service.generate_reply("100", "如何评价故障机器人"),
            "可以从剧情和玩法两方",
        )
        self.assertEqual([enabled for _, enabled in service.requests], [True, False])

    async def test_repeated_toolcall_leak_is_rejected(self) -> None:
        service = FakeAIReplyService(["<toolcall>搜索</toolcall>", "<tool_call>搜索</tool_call>"])
        with self.assertRaisesRegex(AIServiceError, "内部工具调用标记"):
            await service.generate_reply("100", "问题")
        self.assertEqual(len(service.requests), 2)

    async def test_toolcall_leak_without_search_is_rejected(self) -> None:
        service = FakeAIReplyService(["<toolcall>搜索</toolcall>"], web_search_enabled=False)
        with self.assertRaisesRegex(AIServiceError, "内部工具调用标记"):
            await service.generate_reply("100", "问题")
        self.assertEqual(len(service.requests), 1)

    async def test_internal_system_tag_is_rejected(self) -> None:
        service = FakeAIReplyService(
            ["<system>内部内容</system>普通回答"], web_search_enabled=False
        )
        with self.assertRaisesRegex(AIServiceError, "内部工具调用标记"):
            await service.generate_reply("100", "今天是几号")


class ResponsesAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = AIReplyService(AIConfig(api_key="test-key"))
        self.messages = [{"role": "user", "content": "今天有什么新闻"}]

    def test_payload_offers_executable_search_function(self) -> None:
        with patch("ai_client.current_china_date", return_value=date(2026, 9, 28)):
            payload = self.service._build_response_payload(self.messages, web_search_enabled=True)
        self.assertEqual(payload["tools"][0]["type"], "function")
        self.assertEqual(payload["tools"][0]["name"], "web_search")
        self.assertEqual(payload["tools"][0]["parameters"]["required"], ["query"])
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertEqual(payload["input"], self.messages)
        self.assertIn("不要附来源列表", payload["instructions"])
        self.assertIn("2026-09-28", payload["instructions"])
        self.assertEqual(payload["max_output_tokens"], 500)

    def test_casual_chat_keeps_optional_search(self) -> None:
        payload = self.service._build_response_payload(
            [{"role": "user", "content": "你好"}], web_search_enabled=True
        )
        self.assertEqual(payload["tool_choice"], "auto")

    def test_payload_can_disable_web_search(self) -> None:
        payload = self.service._build_response_payload(self.messages, web_search_enabled=False)
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)

    def test_extracts_message_text_and_ignores_tool_items(self) -> None:
        data: dict[str, Any] = {
            "status": "completed",
            "output": [
                {"type": "reasoning", "content": []},
                {"type": "web_search_call", "status": "completed"},
                {"type": "message", "content": [{"type": "output_text", "text": "回答"}]},
            ],
        }
        self.assertEqual(self.service._extract_response_text(data), "回答")

    def test_rejects_incomplete_or_empty_response(self) -> None:
        with self.assertRaises(AIServiceError):
            self.service._extract_response_text({"status": "incomplete", "output": []})
        with self.assertRaises(AIServiceError):
            self.service._extract_response_text({"status": "completed", "output": []})


class FakeSearch:
    def __init__(self, results: list[dict[str, str]]) -> None:
        self.results = results
        self.queries: list[str] = []

    async def search(self, query: str) -> list[dict[str, str]]:
        self.queries.append(query)
        return self.results


class FakeResponsesService(AIReplyService):
    def __init__(self, responses: list[dict[str, Any]], *, base_url: str = "https://api.deepseek.com") -> None:
        super().__init__(AIConfig(api_key="test-key", base_url=base_url))
        self.responses = responses
        self.payloads: list[dict[str, Any]] = []
        self.search_service = FakeSearch([{"title": "新闻", "url": "https://example.com/news"}])

    async def _post_response(self, payload: dict[str, Any]) -> Any:
        self.payloads.append(payload)
        return self.responses.pop(0)


class SearchFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_limit_retries_once_for_proactive_reply(self) -> None:
        service = FakeResponsesService(
            [
                {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []},
                {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "接上话了"}]}]},
            ]
        )
        self.assertEqual(await service.generate_reply("test-group", "聊天"), "接上话了")
        self.assertEqual([item["max_output_tokens"] for item in service.payloads], [500, 4096])

    async def test_non_token_incomplete_response_is_not_retried(self) -> None:
        service = FakeResponsesService(
            [{"status": "incomplete", "incomplete_details": {"reason": "content_filter"}, "output": []}]
        )
        with self.assertRaisesRegex(AIServiceError, "未完成"):
            await service.generate_reply("test-group", "聊天")
        self.assertEqual(len(service.payloads), 1)

    async def test_real_search_results_are_passed_to_final_answer(self) -> None:
        service = FakeResponsesService(
            [
                {
                    "status": "completed",
                    "output": [{"type": "function_call", "call_id": "call-1", "name": "web_search", "arguments": '{"query":"最新消息"}'}],
                },
                {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "查到的回答"}]}]},
            ]
        )
        self.assertEqual(await service.generate_reply("test-group", "有什么最新消息"), "查到的回答")
        self.assertEqual(service.search_service.queries, ["最新消息"])
        self.assertEqual(len(service.payloads), 2)
        self.assertEqual(service.payloads[0]["tools"][0]["name"], "web_search")
        self.assertNotIn("tools", service.payloads[1])
        tool_output = service.payloads[1]["input"][-1]
        self.assertEqual(tool_output["type"], "function_call_output")
        self.assertEqual(tool_output["call_id"], "call-1")
        self.assertEqual(json.loads(tool_output["output"])["results"][0]["url"], "https://example.com/news")

    async def test_parallel_search_calls_are_all_resolved(self) -> None:
        service = FakeResponsesService(
            [
                {"status": "completed", "output": [
                    {"type": "function_call", "call_id": "call-1", "name": "web_search", "arguments": '{"query":"最新消息"}'},
                    {"type": "function_call", "call_id": "call-2", "name": "web_search", "arguments": '{"query":"最新消息"}'},
                ]},
                {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "综合回答"}]}]},
            ]
        )
        self.assertEqual(await service.generate_reply("test-group", "请搜索"), "综合回答")
        self.assertEqual(service.search_service.queries, ["最新消息"])
        outputs = [item for item in service.payloads[1]["input"] if item.get("type") == "function_call_output"]
        self.assertEqual([item["call_id"] for item in outputs], ["call-1", "call-2"])

    async def test_no_search_call_does_not_contact_search_service(self) -> None:
        service = FakeResponsesService(
            [{"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "闲聊回答"}]}]}]
        )
        self.assertEqual(await service.generate_reply("test-group", "你好"), "闲聊回答")
        self.assertEqual(service.search_service.queries, [])

    async def test_requested_search_runs_even_when_model_skips_tool_call(self) -> None:
        service = FakeResponsesService(
            [
                {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "旧消息"}]}]},
                {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "根据搜索结果回答"}]}]},
            ]
        )
        self.assertEqual(
            await service.generate_reply("test-group", "请搜索最新新闻"),
            "根据搜索结果回答"[: service.config.max_reply_chars],
        )
        self.assertEqual(service.search_service.queries, ["请搜索最新新闻"])
        self.assertNotIn("tools", service.payloads[1])
        self.assertIn("实际联网搜索结果", service.payloads[1]["input"][-1]["content"][0]["text"])

    async def test_invalid_search_call_is_not_executed(self) -> None:
        service = FakeResponsesService(
            [{"status": "completed", "output": [{"type": "function_call", "call_id": "call-1", "name": "web_search", "arguments": "not-json"}]}]
        )
        with self.assertRaisesRegex(AIServiceError, "搜索参数无效"):
            await service.generate_reply("test-group", "搜索")
        self.assertEqual(service.search_service.queries, [])

    async def test_search_key_is_not_sent_to_a_different_provider(self) -> None:
        service = FakeResponsesService(
            [{"status": "completed", "output": [{"type": "function_call", "call_id": "call-1", "name": "web_search", "arguments": '{"query":"消息"}'}]}],
            base_url="https://example.com",
        )
        with self.assertRaisesRegex(AIServiceError, "DeepSeek 官方 API 地址"):
            await service.generate_reply("test-group", "搜索")
        self.assertEqual(service.search_service.queries, [])


if __name__ == "__main__":
    unittest.main()
