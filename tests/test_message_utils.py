import unittest

from message_utils import (
    extract_mentioned_ids,
    extract_plain_text,
    has_reply_segment,
    is_bot_mentioned,
)


class MentionTests(unittest.TestCase):
    def test_detects_array_at_segment(self) -> None:
        message = [
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": "你好"}},
        ]
        self.assertTrue(is_bot_mentioned(message, "", "999"))

    def test_does_not_accept_at_all_or_other_user(self) -> None:
        message = [
            {"type": "at", "data": {"qq": "all"}},
            {"type": "at", "data": {"qq": "123"}},
        ]
        self.assertFalse(is_bot_mentioned(message, "", "999"))

    def test_detects_string_cq_code(self) -> None:
        self.assertTrue(is_bot_mentioned("[CQ:at,qq=999] 测试", "", "999"))


class MessageTextTests(unittest.TestCase):
    def test_array_message_keeps_only_text_segments(self) -> None:
        message = [
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": "  你好 "}},
            {"type": "image", "data": {"file": "secret.jpg"}},
            {"type": "text", "data": {"text": " 世界  "}},
        ]
        self.assertEqual(extract_plain_text(message), "你好 世界")

    def test_string_message_removes_cq_codes(self) -> None:
        message = "[CQ:at,qq=999] 请回答 [CQ:image,file=abc.jpg]"
        self.assertEqual(extract_plain_text(message), "请回答")

    def test_raw_message_is_used_as_fallback(self) -> None:
        self.assertEqual(
            extract_plain_text(None, "[CQ:at,qq=999] 测试"),
            "测试",
        )

    def test_mentions_and_reply_metadata_are_extracted_without_text_leakage(self) -> None:
        message = [
            {"type": "reply", "data": {"id": "previous"}},
            {"type": "at", "data": {"qq": "999"}},
            {"type": "at", "data": {"qq": "123"}},
            {"type": "text", "data": {"text": "问题"}},
        ]
        self.assertEqual(extract_mentioned_ids(message), ["999", "123"])
        self.assertTrue(has_reply_segment(message))
        self.assertEqual(extract_plain_text(message), "问题")
        self.assertEqual(extract_mentioned_ids("[CQ:at,qq=123] [CQ:at,qq=999]"), ["123", "999"])
        self.assertTrue(has_reply_segment("[CQ:reply,id=previous] 问题"))


if __name__ == "__main__":
    unittest.main()
