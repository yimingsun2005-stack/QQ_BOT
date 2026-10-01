import asyncio
import base64
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

try:
    import aiohttp  # noqa: F401
except ModuleNotFoundError:
    sys.modules["aiohttp"] = types.SimpleNamespace(ClientWebSocketResponse=object)

from ai_client import AIConfig, AIServiceError
from bot import AIBot, AutoReplyConfig, BotConfig
from music_service import MusicConfig, MusicServiceError, MusicTrack


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


async def wait_for_first_send(websocket):
    while not websocket.sent:
        await asyncio.sleep(0)


class FakeAIService:
    def __init__(self, reply="AI回答", error=None) -> None:
        self.reply = reply
        self.error = error
        self.calls = []

    async def generate_reply(self, group_id, prompt, context=None):
        self.calls.append((group_id, prompt, context))
        if self.error:
            raise self.error
        return self.reply


class FakeMusicService:
    def __init__(self, track=None, error=None) -> None:
        self.track = track
        self.error = error
        self.calls = []

    async def search(self, query):
        self.calls.append(query)
        if self.error:
            raise self.error
        return self.track


def make_config(**overrides) -> BotConfig:
    values = {
        "ws_url": "ws://localhost:4321",
        "access_token": "",
        "allowed_groups": frozenset({"100"}),
        "cooldown_seconds": 5,
        "at_sender": False,
        "ai": AIConfig(api_key="test-key"),
        "music": MusicConfig(),
    }
    values.update(overrides)
    return BotConfig(**values)


def group_event(message, user_id="123", group_id="100", message_id="msg-1") -> dict:
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": "999",
        "group_id": group_id,
        "user_id": user_id,
        "message_id": message_id,
        "message": message,
        "raw_message": "",
    }


class BotEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_message_is_cached_even_when_sampling_skips(self) -> None:
        websocket = FakeWebSocket()
        bot = AIBot(make_config())
        with patch("bot.random.random", return_value=0.9):
            await bot._handle_event(
                websocket,
                group_event([{"type": "text", "data": {"text": "群友在聊的事"}}]),
            )
        self.assertEqual(websocket.sent, [])
        context = bot.context.recent("100")
        self.assertEqual(context[0].text, "群友在聊的事")

    async def test_normal_message_samples_and_supplies_group_context(self) -> None:
        websocket = FakeWebSocket()
        ai_service = FakeAIService("接一句")
        bot = AIBot(make_config(cooldown_seconds=0), ai_service=ai_service)
        with patch("bot.random.random", return_value=0.05):
            await bot._handle_event(
                websocket,
                group_event(
                    [{"type": "text", "data": {"text": "今天天气不错"}}],
                    message_id="m1",
                ),
            )
        self.assertEqual(len(ai_service.calls), 1)
        self.assertEqual(ai_service.calls[0][1], "今天天气不错")
        self.assertEqual(ai_service.calls[0][2][-1].text, "今天天气不错")
        self.assertEqual(len(websocket.sent), 1)

    async def test_mention_bypasses_random_sampling(self) -> None:
        websocket = FakeWebSocket()
        bot = AIBot(make_config(cooldown_seconds=0), ai_service=FakeAIService())
        with patch("bot.random.random", return_value=1.0) as random_draw:
            await bot._handle_event(
                websocket,
                group_event([{"type": "at", "data": {"qq": "999"}}]),
            )
        random_draw.assert_not_called()
        self.assertEqual(len(websocket.sent), 1)

    async def test_speakers_and_mentions_are_distinct_without_raw_ids_in_context(self) -> None:
        websocket = FakeWebSocket()
        ai_service = FakeAIService()
        bot = AIBot(make_config(cooldown_seconds=0), ai_service=ai_service)
        first = group_event([{"type": "text", "data": {"text": "第一个人的话题"}}], user_id="123", message_id="first")
        first["sender"] = {"nickname": "同名"}
        second = group_event(
            [
                {"type": "at", "data": {"qq": "999"}},
                {"type": "at", "data": {"qq": "123"}},
                {"type": "text", "data": {"text": "第二个人的问题"}},
            ],
            user_id="456",
            message_id="second",
        )
        second["sender"] = {"nickname": "同名"}
        with patch("bot.random.random", return_value=0.9):
            await bot._handle_event(websocket, first)
        await bot._handle_event(websocket, second)
        context = ai_service.calls[0][2]
        self.assertNotEqual(context[0].speaker, context[1].speaker)
        self.assertEqual(context[0].text, "第一个人的话题")
        self.assertEqual(context[1].text, "第二个人的问题")
        self.assertEqual(context[1].addressing, f"明确@Bot；同时@{bot._member_label('100', '123')}")
        self.assertNotIn("123", str(context))
        self.assertNotIn("456", str(context))
        self.assertNotIn("999", str(context))

    async def test_mention_only_is_current_context_not_previous_speakers_topic(self) -> None:
        websocket = FakeWebSocket()
        ai_service = FakeAIService()
        bot = AIBot(make_config(cooldown_seconds=0), ai_service=ai_service)
        with patch("bot.random.random", return_value=0.9):
            await bot._handle_event(websocket, group_event([{"type": "text", "data": {"text": "旧话题"}}], user_id="123", message_id="old"))
        await bot._handle_event(websocket, group_event([{"type": "at", "data": {"qq": "999"}}], user_id="456", message_id="current"))
        self.assertEqual(ai_service.calls[0][2][-1].text, "（仅@或引用消息，未附文字）")
        self.assertEqual(ai_service.calls[0][2][-1].addressing, "明确@Bot")

    async def test_other_member_mention_only_is_kept_as_background(self) -> None:
        websocket = FakeWebSocket()
        bot = AIBot(make_config())
        with patch("bot.random.random", return_value=0.9):
            await bot._handle_event(
                websocket,
                group_event([{"type": "at", "data": {"qq": "456"}}], message_id="other-at"),
            )
        item = bot.context.recent("100")[-1]
        self.assertEqual(item.text, "（仅@或引用消息，未附文字）")
        self.assertEqual(item.addressing, f"同时@{bot._member_label('100', '456')}")
        self.assertEqual(websocket.sent, [])

    async def test_disabling_auto_reply_keeps_context_and_mention_replies(self) -> None:
        websocket = FakeWebSocket()
        ai_service = FakeAIService()
        bot = AIBot(
            make_config(
                cooldown_seconds=0,
                auto_reply=AutoReplyConfig(enabled=False),
            ),
            ai_service=ai_service,
        )
        with patch("bot.random.random", side_effect=AssertionError("不应进行随机抽样")):
            await bot._handle_event(
                websocket,
                group_event(
                    [{"type": "text", "data": {"text": "先聊这个话题"}}],
                    message_id="plain",
                ),
            )
        self.assertEqual(ai_service.calls, [])
        self.assertEqual(bot.context.recent("100")[0].text, "先聊这个话题")
        await bot._handle_event(
            websocket,
            group_event(
                [
                    {"type": "at", "data": {"qq": "999"}},
                    {"type": "text", "data": {"text": "你怎么看？"}},
                ],
                message_id="mention",
            ),
        )
        self.assertEqual(len(ai_service.calls), 1)
        self.assertEqual(ai_service.calls[0][2][0].text, "先聊这个话题")
        self.assertEqual(len(websocket.sent), 1)

    async def test_minimum_group_interval_still_applies_to_mentions(self) -> None:
        websocket = FakeWebSocket()
        bot = AIBot(
            make_config(
                cooldown_seconds=0,
                auto_reply=AutoReplyConfig(min_interval_seconds=30),
            ),
            ai_service=FakeAIService(),
        )
        mention = [{"type": "at", "data": {"qq": "999"}}]
        await bot._handle_event(websocket, group_event(mention, message_id="m1"))
        await bot._handle_event(websocket, group_event(mention, message_id="m2"))
        self.assertEqual(len(websocket.sent), 1)

    async def test_hourly_limit_is_checked_before_mention_override(self) -> None:
        websocket = FakeWebSocket()
        bot = AIBot(
            make_config(
                cooldown_seconds=0,
                auto_reply=AutoReplyConfig(max_replies_per_hour=1),
            ),
            ai_service=FakeAIService(),
        )
        mention = [{"type": "at", "data": {"qq": "999"}}]
        await bot._handle_event(websocket, group_event(mention, message_id="m1"))
        await bot._handle_event(websocket, group_event(mention, message_id="m2"))
        self.assertEqual(len(websocket.sent), 1)

    async def test_recent_bot_reply_does_not_raise_base_probability(self) -> None:
        bot = AIBot(
            make_config(
                auto_reply=AutoReplyConfig(
                    probability=0.1,
                    min_interval_seconds=0,
                )
            )
        )
        bot._last_group_reply_at["100"] = 100.0
        with patch("bot.random.random", return_value=0.11):
            self.assertFalse(bot._should_reply("100", False, 100.0))
            self.assertFalse(bot._should_reply("100", False, 200.0))
        with patch("bot.random.random", return_value=0.09):
            self.assertTrue(bot._should_reply("100", False, 100.0))
            self.assertTrue(bot._should_reply("100", False, 200.0))

    async def test_reply_to_bot_gets_participation_probability(self) -> None:
        bot = AIBot(make_config(auto_reply=AutoReplyConfig(min_interval_seconds=0)))
        bot._bot_message_ids["100"] = __import__("collections").deque(["bot-1"])
        event = group_event(
            [{"type": "reply", "data": {"id": "bot-1"}}], message_id="reply"
        )
        with patch("bot.random.random", return_value=0.49):
            self.assertTrue(bot._should_reply("100", False, 50.0, event))
        with patch("bot.random.random", return_value=0.51):
            self.assertFalse(bot._should_reply("100", False, 50.0, event))

    async def test_group_context_is_isolated(self) -> None:
        websocket = FakeWebSocket()
        bot = AIBot(make_config())
        with patch("bot.random.random", return_value=0.9):
            await bot._handle_event(
                websocket,
                group_event([{"type": "text", "data": {"text": "仅在一群"}}]),
            )
        self.assertEqual(bot.context.recent("100")[0].text, "仅在一群")
        self.assertEqual(bot.context.recent("200"), [])

    async def test_unsupported_group_is_ignored(self) -> None:
        websocket = FakeWebSocket()
        bot = AIBot(make_config())
        await bot._handle_event(
            websocket,
            group_event([{"type": "text", "data": {"text": "外群"}}], group_id="200"),
        )
        self.assertEqual(bot.context.recent("200"), [])

    async def test_unreadable_image_leaves_safe_placeholder_in_context(self) -> None:
        websocket = FakeWebSocket()
        bot = AIBot(make_config())
        with (
            patch.object(bot, "_extract_image_data_url", return_value=None),
            patch("bot.random.random", return_value=0.9),
        ):
            await bot._handle_event(
                websocket,
                group_event(
                    [
                        {"type": "text", "data": {"text": "这图是什么"}},
                        {"type": "image", "data": {"file": "unreadable"}},
                    ]
                ),
            )
        self.assertIn("[图片暂时无法读取]", bot.context.recent("100")[0].text)
        self.assertEqual(websocket.sent, [])

    async def test_image_signature_and_size_are_checked(self) -> None:
        valid_png = b"\x89PNG\r\n\x1a\n" + b"image"
        url = AIBot._make_image_data_url(valid_png)
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertIsNone(AIBot._make_image_data_url(b"not an image"))
        self.assertIsNone(AIBot._make_image_data_url(b"\xff\xd8\xff" + b"x" * (2 * 1024 * 1024)))

    async def test_cq_string_image_is_available_to_image_understanding(self) -> None:
        websocket = FakeWebSocket()
        ai = FakeAIService()
        bot = AIBot(make_config(cooldown_seconds=0), ai_service=ai)
        png = b"\x89PNG\r\n\x1a\n" + b"image"
        encoded = __import__("base64").b64encode(png).decode("ascii")
        event = group_event(f"[CQ:at,qq=999] 这是什么[CQ:image,file=base64://{encoded}]")
        await bot._handle_event(websocket, event)
        self.assertEqual(len(ai.calls), 1)
        self.assertTrue(ai.calls[0][2][-1].image_data_url.startswith("data:image/png;base64,"))

    async def test_qq_http_image_url_is_downloaded(self) -> None:
        bot = AIBot(make_config(), ai_service=FakeAIService())
        expected = "data:image/png;base64,aGVsbG8="
        with patch.object(bot, "_download_image", return_value=expected) as download:
            image = await bot._extract_image_data_url(
                FakeWebSocket(),
                group_event([{"type": "image", "data": {"url": "http://gchat.qpic.cn/test.png"}}]),
            )
        self.assertEqual(image, expected)
        download.assert_awaited_once_with("http://gchat.qpic.cn/test.png")

    async def test_download_image_reads_every_response_chunk(self) -> None:
        bot = AIBot(make_config(), ai_service=FakeAIService())
        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/"
            "lWQAAAAASUVORK5CYII="
        )

        class ChunkedContent:
            async def iter_chunked(self, _size):
                yield image[:16]
                yield image[16:]

        class FakeResponse:
            status = 200
            content_length = len(image)
            content = ChunkedContent()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def get(self, *_args, **_kwargs):
                return FakeResponse()

        with patch("bot.aiohttp.ClientSession", return_value=FakeSession()):
            downloaded = await bot._download_image("https://gchat.qpic.cn/test.png")
        self.assertEqual(
            downloaded,
            "data:image/png;base64," + base64.b64encode(image).decode("ascii"),
        )

    async def test_untrusted_http_image_host_is_rejected(self) -> None:
        bot = AIBot(make_config(), ai_service=FakeAIService())
        self.assertIsNone(await bot._download_image("http://example.com/test.png"))
        self.assertIsNone(await bot._download_image("https://127.0.0.1/test.png"))

    async def test_duplicate_message_event_is_processed_once(self) -> None:
        websocket = FakeWebSocket()
        bot = AIBot(make_config(cooldown_seconds=0), ai_service=FakeAIService())
        event = group_event([{"type": "at", "data": {"qq": "999"}}])
        await bot._handle_event(websocket, event)
        await bot._handle_event(websocket, event)
        self.assertEqual(len(websocket.sent), 1)

    async def test_same_group_events_remain_ordered_during_async_image_work(self) -> None:
        websocket = FakeWebSocket()
        ai = FakeAIService()
        bot = AIBot(
            make_config(
                cooldown_seconds=0,
                auto_reply=AutoReplyConfig(min_interval_seconds=0),
            ),
            ai_service=ai,
        )
        image_started = asyncio.Event()
        allow_first_event = asyncio.Event()

        async def controlled_image_fetch(_websocket, event):
            if event.get("message_id") == "first":
                image_started.set()
                await allow_first_event.wait()
            return None

        bot._extract_image_data_url = controlled_image_fetch
        first = asyncio.create_task(
            bot._handle_event(
                websocket,
                group_event([{"type": "text", "data": {"text": "第一条"}}], message_id="first"),
            )
        )
        await image_started.wait()
        second = asyncio.create_task(
            bot._handle_event(
                websocket,
                group_event([{"type": "text", "data": {"text": "第二条"}}], message_id="second"),
            )
        )
        allow_first_event.set()
        with patch("bot.random.random", return_value=0):
            await asyncio.gather(first, second)
        self.assertEqual([call[1] for call in ai.calls], ["第一条", "第二条"])

    async def test_onebot_action_response_resolves_matching_request(self) -> None:
        websocket = FakeWebSocket()
        bot = AIBot(make_config())
        request = asyncio.create_task(
            bot._call_onebot_action(websocket, "get_image", {"file": "image-id"})
        )
        while not websocket.sent:
            await asyncio.sleep(0)
        echo = websocket.sent[0]["echo"]
        handled = bot._handle_action_response(
            {"echo": echo, "status": "ok", "data": {"file": "local-image-path"}}
        )
        self.assertTrue(handled)
        self.assertEqual((await request)["data"]["file"], "local-image-path")

    async def test_ai_failure_sends_configured_error_reply(self) -> None:
        websocket = FakeWebSocket()
        config = AIConfig(api_key="test-key", error_reply="稍后再试")
        bot = AIBot(
            make_config(ai=config),
            ai_service=FakeAIService(error=AIServiceError("offline")),
        )
        await bot._handle_event(
            websocket,
            group_event([{"type": "at", "data": {"qq": "999"}}]),
        )
        self.assertIn("稍后再试", str(websocket.sent[0]["params"]["message"]))

    async def test_proactive_ai_failure_does_not_send_error_or_use_quota(self) -> None:
        websocket = FakeWebSocket()
        bot = AIBot(
            make_config(cooldown_seconds=0),
            ai_service=FakeAIService(error=AIServiceError("offline")),
        )
        with patch("bot.random.random", return_value=0):
            await bot._handle_event(
                websocket,
                group_event([{"type": "text", "data": {"text": "群聊"}}]),
            )
        self.assertEqual(websocket.sent, [])
        self.assertEqual(len(bot._group_reply_times["100"]), 0)

    async def test_music_command_remains_available_when_mentioned(self) -> None:
        websocket = FakeWebSocket()
        music = FakeMusicService(
            MusicTrack(song_id="123456", title="一首歌", artists="歌手")
        )
        ai = FakeAIService()
        bot = AIBot(
            make_config(cooldown_seconds=0), ai_service=ai, music_service=music
        )
        message = [
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": " 点歌一首歌"}},
        ]
        request = asyncio.create_task(bot._handle_event(websocket, group_event(message)))
        await asyncio.wait_for(wait_for_first_send(websocket), timeout=1)
        self.assertFalse(request.done())
        bot._handle_action_response({
            "echo": websocket.sent[0]["echo"],
            "status": "ok", "retcode": 0, "data": {"message_id": "music-confirmed"},
        })
        await asyncio.wait_for(request, timeout=1)
        self.assertEqual(music.calls, ["一首歌"])
        self.assertEqual(ai.calls, [])
        self.assertEqual(len(websocket.sent), 1)
        self.assertEqual(websocket.sent[0]["params"]["message"][0]["type"], "music")
        self.assertIn("music-confirmed", bot._bot_message_ids["100"])

    async def test_music_card_rejected_or_unconfirmed_has_friendly_reply(self) -> None:
        responses = [
            {"status": "failed", "retcode": 1200, "message": "private-provider-error"},
            {"status": "ok", "retcode": 1200, "data": {"message_id": "not-sent"}},
            {"status": "ok", "retcode": 0, "data": {}},
        ]
        for response in responses:
            with self.subTest(response=response):
                websocket = FakeWebSocket()
                bot = AIBot(make_config(), music_service=FakeMusicService(
                    MusicTrack(song_id="123456", title="测试歌", artists="测试歌手")
                ))
                request = asyncio.create_task(bot._handle_music_request(
                    websocket, "100", "123", "测试歌"
                ))
                await asyncio.wait_for(wait_for_first_send(websocket), timeout=1)
                bot._handle_action_response({"echo": websocket.sent[0]["echo"], **response})
                await asyncio.wait_for(request, timeout=1)
                self.assertEqual(len(websocket.sent), 2)
                text = str(websocket.sent[1]["params"]["message"])
                self.assertIn("音乐卡片发送失败或未收到确认", text)
                self.assertNotIn("private-provider-error", text)
                self.assertFalse(bot._bot_message_ids.get("100"))
                self.assertEqual(bot._pending_actions, {})

    async def test_music_card_timeout_has_friendly_reply_without_retry(self) -> None:
        websocket = FakeWebSocket()
        bot = AIBot(make_config(), music_service=FakeMusicService(
            MusicTrack(song_id="123456", title="测试歌", artists="测试歌手")
        ))
        with patch.object(bot, "_call_onebot_action", new=AsyncMock(
            side_effect=asyncio.TimeoutError()
        )) as send:
            await bot._handle_music_request(websocket, "100", "123", "测试歌")
        send.assert_awaited_once()
        self.assertEqual(len(websocket.sent), 1)
        self.assertIn("未收到确认", str(websocket.sent[0]["params"]["message"]))
        self.assertFalse(bot._bot_message_ids.get("100"))

    async def test_music_failure_has_friendly_reply(self) -> None:
        websocket = FakeWebSocket()
        bot = AIBot(
            make_config(),
            music_service=FakeMusicService(error=MusicServiceError("offline")),
        )
        await bot._handle_event(
            websocket,
            group_event(
                [
                    {"type": "at", "data": {"qq": "999"}},
                    {"type": "text", "data": {"text": " 点歌测试"}},
                ]
            ),
        )
        self.assertIn("暂时不可用", websocket.sent[0]["params"]["message"][0]["data"]["text"])


if __name__ == "__main__":
    unittest.main()
