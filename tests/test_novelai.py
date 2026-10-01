import asyncio
import base64
import json
import os
import ssl
import struct
import tempfile
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp

from bot import AIBot, AutoReplyConfig, BotConfig
from message_utils import extract_plain_text
from novelai_image_service import (
    API_URL, HEAVY_NEGATIVE_PROMPT, MODEL, SAMPLERS,
    NovelAIImageConfig, NovelAIImageService, NovelAIServiceError,
    parse_image_command, parse_image_group_ids, validate_png,
)
from test_bot import FakeAIService, FakeWebSocket, group_event, make_config


def png(data=b"\x00\x01\x02\x03"):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(data)) + chunk(b"IEND", b""))


def response_body(image=None):
    return json.dumps({"images": [{"image": base64.b64encode(image or png()).decode(), "seed": 1}]}).encode()


class ConfigAndPayloadTests(unittest.TestCase):
    def test_defaults_and_prompt_preservation(self):
        config = NovelAIImageConfig()
        self.assertFalse(config.enabled)
        self.assertEqual((config.width, config.height, config.steps, config.guidance), (832, 1216, 23, 7))
        service = NovelAIImageService(config)
        prompt = "1girl,  1.5::sunset::\n{blue eyes}, [rain]"
        with patch("novelai_image_service.secrets.randbelow", side_effect=[123, 456]):
            first, second = service.build_payload(prompt), service.build_payload(prompt)
        self.assertEqual(first["model"], MODEL)
        self.assertEqual(first["input"], prompt)
        p = first["parameters"]
        self.assertEqual(p["v4_prompt"]["caption"], {"base_caption": prompt, "char_captions": []})
        self.assertEqual(p["negative_prompt"], HEAVY_NEGATIVE_PROMPT)
        self.assertEqual(p["v4_negative_prompt"]["caption"]["base_caption"], HEAVY_NEGATIVE_PROMPT)
        self.assertEqual((p["params_version"], p["n_samples"], p["seed"], second["parameters"]["seed"]), (4, 1, 123, 456))
        self.assertFalse(p["qualityToggle"])
        for name, sampler in SAMPLERS.items():
            with self.subTest(name=name):
                payload = NovelAIImageService(replace(config, sampler=sampler, width=1024, height=1024,
                                                     steps=30, guidance=5.5)).build_payload("test")
                p = payload["parameters"]
                self.assertEqual((p["sampler"], p["width"], p["height"], p["steps"], p["scale"]),
                                 (sampler, 1024, 1024, 30, 5.5))

    def test_invalid_config_values_and_boundaries(self):
        for fields in [dict(enabled=1), dict(token="x\ny"), dict(width=832.5), dict(width=True),
                       dict(width=65), dict(height=0), dict(height=2112), dict(width=2048, height=2048),
                       dict(steps=0), dict(steps=51), dict(steps=1.5), dict(guidance=float("nan")),
                       dict(guidance=float("inf")), dict(guidance=11), dict(guidance=-1),
                       dict(sampler="unknown"), dict(timeout_seconds=0), dict(timeout_seconds=301)]:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                replace(NovelAIImageConfig(), **fields).validate()
        for fields in [dict(width=64, height=64, steps=1, guidance=0),
                       dict(width=2048, height=1024, steps=50, guidance=10)]:
            replace(NovelAIImageConfig(), **fields).validate()
        self.assertNotIn("dummy-token", repr(NovelAIImageConfig(token="dummy-token")))
        with self.assertRaises(ValueError):
            NovelAIImageConfig.from_mapping([])

    def test_command_and_text_extraction(self):
        self.assertEqual(parse_image_command("画图"), "")
        self.assertEqual(parse_image_command("画图   \n"), "")
        self.assertIsNone(parse_image_command("请画图 cat"))
        self.assertIsNone(parse_image_command("画图cat"))
        source = "[CQ:at,qq=999]画图  1girl,  1.5::sunset::\n&#91;rain&#93;"
        prompt = parse_image_command(extract_plain_text(source, normalize_whitespace=False))
        self.assertEqual(prompt, "1girl,  1.5::sunset::\n[rain]")

    def test_image_group_input_parsing_and_validation(self):
        self.assertEqual(parse_image_group_ids("100, 200\n100；300，400 500"),
                         ["100", "200", "300", "400", "500"])
        self.assertEqual(parse_image_group_ids(" \n "), [])
        for value in ("100, invalid", "１００", "100/200"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_image_group_ids(value)
        for groups in ("100", [100], [""], ["invalid"], ["１００"], None):
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                NovelAIImageConfig.from_mapping({"allowed_groups": groups})
        self.assertEqual(NovelAIImageConfig.from_mapping({}).allowed_groups, [])

    def test_valid_png_and_invalid_responses(self):
        self.assertEqual(NovelAIImageService.parse_response(response_body()), png())
        for body in [b"not-json", b"null", b"[]", b'{}', b'{"images": []}',
                     b'{"images": [{"image": "!invalid!"}]}', response_body(b"not-png"),
                     response_body(png()[:-1]), response_body(png() + b"extra"),
                     response_body(png(b"\x00" * 20000)), response_body(png(b"\x05\x00\x00\x00"))]:
            with self.subTest(body_length=len(body)), self.assertRaises(NovelAIServiceError):
                NovelAIImageService.parse_response(body)
        corrupt = bytearray(png())
        corrupt[20] ^= 1
        with self.assertRaises(NovelAIServiceError):
            validate_png(bytes(corrupt))


class Response:
    def __init__(self, status=201, body=None, error=None):
        self.status = status
        self.body = response_body() if body is None else body
        self.error = error
        self.content = self

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *args):
        return False

    async def iter_chunked(self, size):
        for offset in range(0, len(self.body), size):
            yield self.body[offset:offset + size]


class Session:
    def __init__(self, response):
        self.response = response
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.response


class ServiceRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_headers_request_format_and_image(self):
        session = Session(Response())
        with patch("novelai_image_service.aiohttp.ClientSession", return_value=session) as factory:
            image = await NovelAIImageService(NovelAIImageConfig(token="dummy-token")).generate("1girl")
        self.assertEqual(image, png())
        headers = factory.call_args.kwargs["headers"]
        self.assertEqual(headers, {"Authorization": "Bearer dummy-token", "Accept": "application/json"})
        self.assertEqual(factory.call_args.kwargs["timeout"].total, 180)
        self.assertTrue(factory.call_args.kwargs["trust_env"])
        self.assertEqual(session.requests[0][0], API_URL)
        self.assertFalse(session.requests[0][1]["allow_redirects"])
        self.assertEqual(session.requests[0][1]["json"]["input"], "1girl")

    async def test_status_network_timeout_and_no_retry_or_error_leak(self):
        cases = [(Response(status=401), "Token"), (Response(status=403), "Token"),
                 (Response(status=402), "额度"), (Response(status=429), "频繁"),
                 (Response(status=500), "暂时不可用"), (Response(status=302), "暂时不可用"),
                 (Response(error=asyncio.TimeoutError("private-response")), "超时"),
                 (Response(error=aiohttp.ClientError("private-response")), "无法连接")]
        for response, expected in cases:
            with self.subTest(expected=expected):
                session = Session(response)
                with patch("novelai_image_service.aiohttp.ClientSession", return_value=session):
                    with self.assertRaises(NovelAIServiceError) as caught:
                        await NovelAIImageService(NovelAIImageConfig(token="dummy-token")).generate("cat")
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn("private-response", str(caught.exception))
                self.assertEqual(len(session.requests), 1)

    async def test_invalid_prompt_missing_token_and_response_limits(self):
        with patch("novelai_image_service.aiohttp.ClientSession") as factory:
            for prompt in ("", "x" * 4001, "cat"):
                with self.assertRaises(NovelAIServiceError):
                    await NovelAIImageService(NovelAIImageConfig()).generate(prompt)
            factory.assert_not_called()
        session = Session(Response(body=b"x" * 100))
        with patch("novelai_image_service.aiohttp.ClientSession", return_value=session), \
                patch("novelai_image_service.MAX_RESPONSE_BYTES", 50):
            with self.assertRaisesRegex(NovelAIServiceError, "过大"):
                await NovelAIImageService(NovelAIImageConfig(token="dummy-token")).generate("cat")

    async def test_system_proxy_is_used_for_generation_request(self):
        requests = []
        async def proxy(reader, writer):
            try:
                headers = await reader.readuntil(b"\r\n\r\n")
                length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                              if line.lower().startswith(b"content-length:"))
                body = await reader.readexactly(length)
                requests.append((headers, json.loads(body)))
                response = response_body()
                writer.write(b"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\n"
                             + f"Content-Length: {len(response)}\r\nConnection: close\r\n\r\n".encode()
                             + response)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(proxy, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            with patch.dict(os.environ, {
                "HTTP_PROXY": f"http://127.0.0.1:{port}", "http_proxy": f"http://127.0.0.1:{port}",
                "NO_PROXY": "", "no_proxy": "",
            }), patch("novelai_image_service.API_URL", "http://novelai.invalid/ai/generate-image"):
                image = await NovelAIImageService(NovelAIImageConfig(token="dummy-token")).generate("cat")
        self.assertEqual(image, png())
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0][0].startswith(b"POST http://novelai.invalid/ai/generate-image "))
        self.assertIn(b"Authorization: Bearer dummy-token", requests[0][0])
        self.assertEqual(requests[0][1]["model"], MODEL)

    async def test_proxy_and_tls_failures_have_safe_specific_messages(self):
        key = SimpleNamespace(host="private-host", port=443, ssl=True)
        cases = [
            (aiohttp.ClientProxyConnectionError(key, OSError(111, "private-response")), "代理"),
            (aiohttp.ClientConnectorCertificateError(key, ssl.CertificateError("private-response")), "校验失败"),
            (aiohttp.ClientConnectorSSLError(key, ssl.SSLError("private-response")), "校验失败"),
        ]
        for error, expected in cases:
            with self.subTest(expected=expected):
                session = Session(Response(error=error))
                with patch("novelai_image_service.aiohttp.ClientSession", return_value=session):
                    with self.assertRaises(NovelAIServiceError) as caught:
                        await NovelAIImageService(NovelAIImageConfig(token="dummy-token")).generate("cat")
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn("private", str(caught.exception))
                self.assertEqual(len(session.requests), 1)


class BotImageTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, **options):
        config = make_config(cooldown_seconds=0, auto_reply=AutoReplyConfig(enabled=False),
                             novelai_image_generation=NovelAIImageConfig(enabled=True, token="dummy-token",
                                                                        allowed_groups=["100", "200"]), **options)
        self.ai = FakeAIService()
        self.service = type("Service", (), {})()
        self.service.generate = AsyncMock(return_value=png())
        self.bot = AIBot(config, ai_service=self.ai, image_service=self.service)
        self.bot._call_onebot_action = AsyncMock(return_value={"status": "ok", "retcode": 0,
                                                               "data": {"message_id": "result"}})
        self.ws = FakeWebSocket()
        return self.bot

    async def finish(self):
        if self.bot._image_task is not None:
            await asyncio.gather(self.bot._image_task, return_exceptions=True)
        await asyncio.sleep(0)

    async def asyncTearDown(self):
        if hasattr(self, "bot"):
            self.bot.stop()
            await self.finish()

    async def command(self, text="画图 cat", message_id="one", **kwargs):
        await self.bot._handle_event(self.ws, group_event([
            {"type": "at", "data": {"qq": "999"}}, {"type": "text", "data": {"text": text}},
        ], message_id=message_id, **kwargs))

    async def test_array_and_cq_forms_send_image_without_ai_or_sampling(self):
        for message in [
            [{"type": "at", "data": {"qq": "999"}}, {"type": "text", "data": {"text": " 画图 1girl,  1.5::sunset::\n[rain]"}}],
            "[CQ:at,qq=999]画图 1girl,  1.5::sunset::\n&#91;rain&#93;",
        ]:
            self.make_bot(at_sender=True)
            with patch("bot.random.random") as draw:
                await self.bot._handle_event(self.ws, group_event(message))
                await self.finish()
                draw.assert_not_called()
            self.service.generate.assert_awaited_once_with("1girl,  1.5::sunset::\n[rain]")
            self.assertFalse(self.ai.calls)
            payload = self.bot._call_onebot_action.call_args.args
            self.assertEqual(payload[1], "send_group_msg")
            self.assertEqual(payload[2]["group_id"], "100")
            self.assertEqual(payload[2]["message"][0]["type"], "at")
            encoded = payload[2]["message"][-1]["data"]["file"]
            self.assertEqual(base64.b64decode(encoded.removeprefix("base64://")), png())
            self.assertIn("result", self.bot._bot_message_ids["100"])
            self.assertFalse(self.bot._image_busy)
            self.assertIsNone(self.bot._image_task)

    async def test_no_mention_other_mention_private_and_disallowed_group(self):
        self.make_bot()
        for message in ["画图 cat", "@bot画图 cat", "[CQ:at,qq=888]画图 cat"]:
            await self.bot._handle_event(self.ws, group_event(message, message_id=message))
        await self.command(group_id="300")
        private = group_event("[CQ:at,qq=999]画图 cat", message_id="private")
        private["message_type"] = "private"
        await self.bot._handle_event(self.ws, private)
        self.service.generate.assert_not_awaited()

    async def test_image_only_group_can_draw_without_enabling_chat_or_music(self):
        self.make_bot()
        await self.command(group_id="200", message_id="drawing-only")
        await self.finish()
        self.service.generate.assert_awaited_once_with("cat")
        self.assertEqual(self.bot._call_onebot_action.call_args.args[2]["group_id"], "200")
        before = len(self.ws.sent)
        await self.command("聊聊天", group_id="200", message_id="chat-not-enabled")
        await self.command("点歌 测试", group_id="200", message_id="music-not-enabled")
        self.assertEqual(len(self.ws.sent), before)
        self.assertFalse(self.ai.calls)
        self.assertEqual(self.bot.context.recent("200"), [])

    async def test_chat_group_without_image_permission_does_not_generate(self):
        self.make_bot()
        self.bot.config = replace(self.bot.config, novelai_image_generation=replace(
            self.bot.config.novelai_image_generation, allowed_groups=["200"]))
        await self.command()
        self.service.generate.assert_not_awaited()
        self.assertFalse(self.ai.calls)
        self.assertIn("本群未开启画图", self.ws.sent[-1]["params"]["message"][-1]["data"]["text"])
        await self.command("你好", message_id="chat-still-enabled")
        self.assertEqual(len(self.ai.calls), 1)

    async def test_empty_image_list_does_not_mean_all_groups(self):
        self.make_bot()
        self.bot.config = replace(self.bot.config, allowed_groups=frozenset(),
                                 novelai_image_generation=replace(self.bot.config.novelai_image_generation,
                                                                 allowed_groups=[]))
        for group_id in ("100", "200"):
            await self.command(group_id=group_id, message_id=group_id)
        self.assertFalse(self.ws.sent)
        self.service.generate.assert_not_awaited()
        self.assertFalse(self.ai.calls)

    async def test_disabled_missing_prompt_token_and_too_long_do_not_call_ai(self):
        self.make_bot()
        for config, prompt, expected in [
            (NovelAIImageConfig(), "画图 cat", "未开启"),
            (NovelAIImageConfig(enabled=True, token="dummy-token", allowed_groups=["100"]), "画图", "提示词"),
            (NovelAIImageConfig(enabled=True, allowed_groups=["100"]), "画图 cat", "Token"),
            (NovelAIImageConfig(enabled=True, token="dummy-token", allowed_groups=["100"]), "画图 " + "x" * 4001, "4000"),
        ]:
            self.bot.config = replace(self.bot.config, novelai_image_generation=config)
            await self.command(prompt, message_id=expected)
            self.assertIn(expected, self.ws.sent[-1]["params"]["message"][-1]["data"]["text"])
        self.service.generate.assert_not_awaited()
        self.assertFalse(self.ai.calls)

    async def test_busy_across_groups_and_chat_is_not_blocked(self):
        self.make_bot()
        self.bot.config = replace(self.bot.config, allowed_groups=frozenset({"100", "200"}))
        started, release = asyncio.Event(), asyncio.Event()
        async def slow(prompt):
            started.set()
            await release.wait()
            return png()
        self.service.generate.side_effect = slow
        await self.command()
        await started.wait()
        await asyncio.wait_for(self.command(message_id="two", group_id="200"), 1)
        self.assertIn("稍后再试", self.ws.sent[-1]["params"]["message"][-1]["data"]["text"])
        await asyncio.wait_for(self.command("聊聊天", message_id="chat"), 1)
        self.assertEqual(len(self.ai.calls), 1)
        self.assertEqual(self.service.generate.await_count, 1)
        release.set()
        await self.finish()

    async def test_dedupe_cooldown_and_subsequent_request(self):
        self.make_bot()
        await self.command()
        await self.finish()
        await self.command()
        self.assertEqual(self.service.generate.await_count, 1)
        self.bot.config = replace(self.bot.config, cooldown_seconds=60)
        await self.command(message_id="cooldown")
        self.assertEqual(self.service.generate.await_count, 1)
        self.bot.config = replace(self.bot.config, cooldown_seconds=0)
        await self.command(message_id="next")
        await self.finish()
        self.assertEqual(self.service.generate.await_count, 2)

    async def test_image_request_during_cooldown_receives_wait_hint(self):
        self.make_bot()
        self.bot.config = replace(self.bot.config, cooldown_seconds=5)
        await self.command()
        await self.finish()
        before = len(self.ws.sent)
        await self.command(message_id="during-cooldown")
        self.assertEqual(len(self.ws.sent), before + 1)
        text = self.ws.sent[-1]["params"]["message"][-1]["data"]["text"]
        self.assertIn("秒后再试", text)
        self.service.generate.assert_awaited_once()

    async def test_image_request_does_not_wait_for_same_group_chat(self):
        self.make_bot()
        self.bot.config = replace(self.bot.config, at_sender=True)
        started, release = asyncio.Event(), asyncio.Event()
        async def slow_chat(group_id, prompt, context):
            started.set()
            await release.wait()
            return "聊天回答"
        self.ai.generate_reply = slow_chat
        chat = asyncio.create_task(self.command("聊聊天", message_id="slow-chat", user_id="456"))
        try:
            await started.wait()
            await asyncio.wait_for(self.command(message_id="image-during-chat"), timeout=1)
            await self.finish()
            self.service.generate.assert_awaited_once_with("cat")
            self.assertEqual(self.bot._call_onebot_action.call_args.args[2]["message"][0]["type"], "at")
        finally:
            release.set()
            await chat

    async def test_generation_failure_and_sending_failure_release_busy(self):
        self.make_bot()
        for error in (NovelAIServiceError("画图超时"), RuntimeError("private-response")):
            self.service.generate.side_effect = error
            await self.command(message_id=type(error).__name__)
            await self.finish()
            text = self.ws.sent[-1]["params"]["message"][-1]["data"]["text"]
            self.assertNotIn("private-response", text)
            self.assertFalse(self.bot._image_busy)
        self.service.generate.side_effect = None
        self.bot._call_onebot_action.side_effect = ValueError("private-response")
        await self.command(message_id="send-fail")
        await self.finish()
        self.assertIn("发送失败", self.ws.sent[-1]["params"]["message"][-1]["data"]["text"])
        self.assertEqual(self.service.generate.await_count, 3)
        self.assertFalse(self.bot._image_busy)

    async def test_stop_cancels_and_releases_busy(self):
        self.make_bot()
        started = asyncio.Event()
        async def slow(prompt):
            started.set()
            await asyncio.Future()
        self.service.generate.side_effect = slow
        await self.command()
        await started.wait()
        self.bot.stop()
        await self.finish()
        self.assertFalse(self.bot._image_busy)
        self.assertFalse(self.bot._event_tasks)
        self.bot._call_onebot_action.assert_not_awaited()

    async def test_disconnect_cancels_generation_and_clears_tasks(self):
        self.make_bot()
        started = asyncio.Event()
        async def slow(prompt):
            started.set()
            await asyncio.Future()
        self.service.generate.side_effect = slow
        await self.command()
        await started.wait()
        class ClosedSocket:
            def __aiter__(self):
                return self
            async def __anext__(self):
                raise StopAsyncIteration
        await self.bot._consume(ClosedSocket())
        await asyncio.sleep(0)
        self.assertFalse(self.bot._image_busy)
        self.assertFalse(self.bot._event_tasks)
        self.assertFalse(self.bot._pending_actions)

    async def test_real_onebot_ack_success_and_rejection(self):
        self.make_bot()
        del self.bot.__dict__["_call_onebot_action"]
        bot = self.bot
        class AckSocket(FakeWebSocket):
            reject = False
            async def send_json(self, payload):
                await super().send_json(payload)
                if any(segment["type"] == "image" for segment in payload["params"]["message"]):
                    bot._handle_action_response({
                        "echo": payload["echo"], "status": "failed" if self.reject else "ok",
                        "retcode": 100 if self.reject else 0,
                        "data": {"message_id": "confirmed"}, "message": "private-response",
                    })
        self.ws = AckSocket()
        await self.command()
        await self.finish()
        self.assertIn("confirmed", self.bot._bot_message_ids["100"])
        self.assertEqual(self.ws.sent[-1]["params"]["message"][0]["type"], "image")
        self.ws.reject = True
        await self.command(message_id="rejected")
        await self.finish()
        text = self.ws.sent[-1]["params"]["message"][-1]["data"]["text"]
        self.assertIn("发送失败", text)
        self.assertNotIn("private-response", text)
        self.assertFalse(self.bot._pending_actions)
        self.assertEqual(self.service.generate.await_count, 2)

    async def test_send_confirmation_timeout_does_not_regenerate(self):
        self.make_bot()
        self.bot._call_onebot_action.side_effect = asyncio.TimeoutError
        await self.command()
        await self.finish()
        self.assertIn("未收到确认", self.ws.sent[-1]["params"]["message"][-1]["data"]["text"])
        self.service.generate.assert_awaited_once()
        self.assertFalse(self.bot._image_busy)

    async def test_immediate_cancel_and_ack_failure_release_busy(self):
        self.make_bot()
        await self.command()
        self.bot.stop()
        await self.finish()
        self.assertFalse(self.bot._image_busy)
        self.make_bot()
        self.ws.send_json = AsyncMock(side_effect=RuntimeError("send-failed"))
        with self.assertRaises(RuntimeError):
            await self.command()
        self.assertFalse(self.bot._image_busy)
        self.service.generate.assert_not_awaited()
