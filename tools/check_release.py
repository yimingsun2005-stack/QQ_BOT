"""Release checks using synthetic configuration and local mock services only."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp import web
from ai_client import AIReplyService, AIServiceError
from bot import AIBot, BotConfig
from group_context import GroupContextStore
from runtime_config import ConfigurationError
from single_instance import AlreadyRunningError, SingleInstance
from build_release import FILES, blank, build


def fixture():
    data = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    data.update(ws_url="ws://127.0.0.1:9", allowed_groups=["10001"])
    data["ai"].update(base_url="http://127.0.0.1:9/v1", api_key="synthetic-test-key", model="synthetic-model")
    return data


def load(data):
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "config.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        before = path.read_bytes()
        try:
            return BotConfig.load(path)
        finally:
            assert before == path.read_bytes(), "configuration was modified"


class ConfigurationChecks(unittest.TestCase):
    def test_template_is_fully_blank(self):
        template = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(template, blank(template))

    def test_required_fields(self):
        for field in ("ws_url", "allowed_groups", "ai.base_url", "ai.api_key", "ai.model"):
            with self.subTest(field=field):
                data = fixture()
                mapping, key = (data["ai"], field[3:]) if field.startswith("ai.") else (data, field)
                mapping[key] = [] if key == "allowed_groups" else ""
                with self.assertRaises(ConfigurationError):
                    load(data)

    def test_empty_values_use_defaults(self):
        config = load(fixture())
        self.assertEqual(config.context.context_messages, 20)
        self.assertEqual(config.reply_limits.min_interval_seconds, 30)
        self.assertEqual(config.ai.max_tokens, 500)
        self.assertTrue(config.at_sender)
        self.assertFalse(config.web_search.enabled)
        self.assertFalse(config.music.enabled)
        self.assertFalse(config.novelai_image_generation.enabled)

    def test_invalid_types_do_not_expose_values(self):
        for field, value in (("allowed_groups", ["PRIVATE-MARKER"]),
                             ("cooldown_seconds", "PRIVATE-MARKER"), ("at_sender", "PRIVATE-MARKER")):
            with self.subTest(field=field):
                data = fixture(); data[field] = value
                with self.assertRaises(ConfigurationError) as error:
                    load(data)
                self.assertNotIn("PRIVATE-MARKER", str(error.exception))
                self.assertIn(field, str(error.exception))

    def test_image_enabled_requires_token(self):
        data = fixture(); data["novelai_image_generation"]["enabled"] = True
        with self.assertRaises(ConfigurationError):
            load(data)

    def test_environment_overrides(self):
        data = fixture(); data["ai"]["api_key"] = ""
        with patch.dict(os.environ, {"AI_API_KEY": "synthetic-env-key", "ONEBOT_WS_URL": "ws://localhost:9"}):
            config = load(data)
        self.assertEqual(config.ai.api_key, "synthetic-env-key")
        self.assertEqual(config.ws_url, "ws://localhost:9")

    def test_legacy_chat_settings_read_without_write(self):
        data = fixture(); data.pop("context"); data.pop("reply_limits")
        data["auto_reply"] = {"enabled": True, "context_messages": 12, "max_replies_per_hour": 80}
        config = load(data)
        self.assertEqual(config.context.context_messages, 12)
        self.assertEqual(config.reply_limits.max_replies_per_hour, 80)

    def test_lock_and_release(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            with SingleInstance(path):
                with self.assertRaises(AlreadyRunningError):
                    with SingleInstance(path):
                        pass
            with SingleInstance(path):
                pass

    def test_packages_are_allowlisted_and_blank(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = build(Path(folder))
            expected = {"QQ_BOT/" + name for name in (*FILES, "config.json")}
            with zipfile.ZipFile(paths[0]) as archive:
                zipped = {name: archive.read(name) for name in archive.namelist()}
            with tarfile.open(paths[1]) as archive:
                tarred = {item.name: archive.extractfile(item).read() for item in archive.getmembers()}
            self.assertEqual(set(zipped), expected)
            self.assertEqual(zipped, tarred)
            for name in ("config.json", "config.example.json"):
                data = json.loads(zipped["QQ_BOT/" + name])
                self.assertEqual(data, blank(data))
            for line in paths[2].read_text().splitlines():
                digest, name = line.split("  ")
                self.assertEqual(digest, hashlib.sha256((Path(folder) / name).read_bytes()).hexdigest())

    def test_messages_payload_and_endpoint(self):
        config = load(fixture())
        service = AIReplyService(config.ai, config.web_search)
        payload = service._build_message_payload(service._build_input("question", []))
        self.assertEqual(service._message_endpoint(), "http://127.0.0.1:9/v1/messages")
        self.assertNotIn("tools", payload)
        service = AIReplyService(config.ai, replace(config.web_search, enabled=True))
        self.assertEqual(service._build_message_payload([])["tools"][0]["name"], "web_search")
        with self.assertRaises(AIServiceError):
            service._extract_message_text({"stop_reason": "max_tokens", "content": []})

    def test_context_isolated_and_bounded(self):
        context = GroupContextStore(max_messages=2, max_age_seconds=30)
        for i in range(3):
            context.add("10001", "member", str(i), now=i)
        self.assertEqual(len(context.recent("10001", now=3)), 2)
        self.assertEqual(context.recent("20001", now=3), [])
        self.assertEqual(context.recent("10001", now=100), [])

    def test_startup_logs_hide_paths_and_raw_errors(self):
        config = fixture(); config["cooldown_seconds"] = "PRIVATE-MARKER"
        with tempfile.TemporaryDirectory(prefix="PRIVATE-PATH-") as folder:
            path = Path(folder) / "config.json"; path.write_text(json.dumps(config))
            process = subprocess.run([sys.executable, str(ROOT / "bot.py")],
                env={**os.environ, "BOT_CONFIG": str(path)}, capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 1)
            self.assertNotIn("PRIVATE-MARKER", process.stderr)
            self.assertNotIn("PRIVATE-PATH-", process.stderr)
            self.assertIn("cooldown_seconds", process.stderr)


class BehaviorChecks(unittest.IsolatedAsyncioTestCase):
    async def test_empty_allowlist_rejects_messages(self):
        bot = AIBot(replace(load(fixture()), allowed_groups=frozenset()))
        bot._dispatch_image_command = AsyncMock()
        bot._process_group_event = AsyncMock()
        await bot._handle_event(AsyncMock(), {"message_type": "group", "group_id": "10001",
            "user_id": "20001", "self_id": "30001", "message": []})
        bot._dispatch_image_command.assert_not_awaited()
        bot._process_group_event.assert_not_awaited()

    async def test_only_mentions_trigger_ai(self):
        bot = AIBot(load(fixture()))
        self.assertFalse(bot._should_reply("10001", False, 100))
        self.assertTrue(bot._should_reply("10001", True, 100))

    async def test_standard_music_without_private_server(self):
        bot = AIBot(load(fixture()))
        from music_service import MusicTrack
        with patch.dict(os.environ, {"MUSIC_SIGN_URL": ""}):
            result = await bot._build_signed_music_segment(MusicTrack("123", "song", "artist"))
        self.assertEqual(result["type"], "music")
        self.assertEqual(result["data"]["type"], "163")

    async def test_full_curated_model_selection(self):
        from novelai_image_service import NovelAIImageConfig
        config = replace(load(fixture()), novelai_image_generation=NovelAIImageConfig(
            enabled=True, token="synthetic-image-key", allowed_groups=["10001"]))
        bot = AIBot(config)
        bot.image_service.generate = AsyncMock(return_value=b"synthetic-image")
        bot._call_onebot_action = AsyncMock(return_value={"data": {"message_id": "1"}})
        for group, model in (("10001", "nai-diffusion-5-full"), ("20001", "nai-diffusion-5-curated")):
            await bot._generate_image(AsyncMock(), group, "30001", "prompt")
            self.assertEqual(bot.image_service.generate.await_args.kwargs["model"], model)

    async def test_image_cancelled_on_stop(self):
        bot = AIBot(load(fixture()))
        bot._image_task = asyncio.create_task(asyncio.sleep(60))
        bot.stop()
        await asyncio.gather(bot._image_task, return_exceptions=True)
        self.assertTrue(bot._image_task.cancelled())

    @unittest.skipUnless(os.name == "posix", "Linux lifecycle check")
    async def test_linux_reconnect_duplicate_sigterm_and_onebot(self):
        received = asyncio.Queue()
        ai_started = asyncio.Event()
        release_ai = asyncio.Event()
        connections = 0
        ai_calls = 0
        async def messages(request):
            nonlocal ai_calls
            data = await request.json(); ai_calls += 1
            self.assertEqual(data["model"], "synthetic-model")
            if ai_calls > 1:
                ai_started.set(); await release_ai.wait()
            return web.json_response({"stop_reason": "end_turn", "content": [{"type": "text", "text": "mock-answer"}]})
        async def websocket(request):
            nonlocal connections
            ws = web.WebSocketResponse(); await ws.prepare(request); connections += 1
            if connections == 1:
                await ws.close(); return ws
            await ws.send_json({"post_type": "message", "message_type": "group", "group_id": "10001",
                "user_id": "20001", "self_id": "30001", "message_id": "1", "message": [
                {"type": "at", "data": {"qq": "30001"}}, {"type": "text", "data": {"text": "question"}}]})
            async for frame in ws:
                action = json.loads(frame.data)
                if action["action"] == "send_group_msg":
                    await received.put(action)
                    await ws.send_json({"echo": action["echo"], "status": "ok", "retcode": 0,
                                        "data": {"message_id": "2"}})
                    await ws.send_json({"post_type": "message", "message_type": "group", "group_id": "10001",
                        "user_id": "20001", "self_id": "30001", "message_id": "3", "message": [
                        {"type": "at", "data": {"qq": "30001"}}, {"type": "text", "data": {"text": "cancel-me"}}]})
            return ws
        app = web.Application(); app.router.add_get("/ws", websocket); app.router.add_post("/v1/messages", messages)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
        port = site._server.sockets[0].getsockname()[1]
        process = None
        try:
            with tempfile.TemporaryDirectory() as folder:
                data = fixture(); data["ws_url"] = f"ws://127.0.0.1:{port}/ws"
                data["ai"]["base_url"] = f"http://127.0.0.1:{port}/v1"
                data["cooldown_seconds"] = 0; data["reply_limits"]["min_interval_seconds"] = 0
                path = Path(folder) / "config.json"; path.write_text(json.dumps(data))
                before = path.read_bytes(); env = {**os.environ, "BOT_CONFIG": str(path)}
                process = await asyncio.create_subprocess_exec(sys.executable, str(ROOT / "bot.py"),
                    env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                reply = await asyncio.wait_for(received.get(), 15)
                self.assertIn("mock-answer", json.dumps(reply))
                self.assertGreaterEqual(connections, 2)
                duplicate = await asyncio.create_subprocess_exec(sys.executable, str(ROOT / "bot.py"),
                    env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await asyncio.wait_for(duplicate.communicate(), 10)
                self.assertEqual(duplicate.returncode, 2)
                await asyncio.wait_for(ai_started.wait(), 5)
                process.send_signal(signal.SIGTERM)
                _, error = await asyncio.wait_for(process.communicate(), 10)
                self.assertEqual(process.returncode, 0)
                for private in (str(port), "10001", "20001", "mock-answer", "synthetic-test-key", "Task was destroyed"):
                    self.assertNotIn(private, error.decode())
                with SingleInstance(path):
                    pass
                self.assertEqual(before, path.read_bytes())
        finally:
            release_ai.set()
            if process and process.returncode is None:
                process.kill(); await process.wait()
            await runner.cleanup()


if __name__ == "__main__":
    # Ignore developer credentials: every check uses isolated synthetic values.
    for name in ("AI_API_KEY", "DEEPSEEK_API_KEY", "ONEBOT_WS_URL", "ONEBOT_ACCESS_TOKEN", "BOT_CONFIG", "MUSIC_SIGN_URL"):
        os.environ.pop(name, None)
    unittest.main(verbosity=2)
