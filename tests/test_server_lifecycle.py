import asyncio
import json
import os
import signal
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from bot import AIBot
from test_bot import FakeAIService, group_event, make_config


class ServerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_websocket_event_reply_reconnect_and_stop(self):
        connected = asyncio.Queue()
        sent = asyncio.Queue()
        async def handler(request):
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await connected.put(socket)
            async for frame in socket:
                await sent.put(json.loads(frame.data))
            return socket
        app = web.Application()
        app.router.add_get('/ws', handler)
        async with TestServer(app) as server:
            bot = AIBot(replace(make_config(), ws_url=str(server.make_url('/ws')).replace('http:', 'ws:')), ai_service=FakeAIService())
            task = asyncio.create_task(bot.run())
            try:
                socket = await asyncio.wait_for(connected.get(), 5)
                await socket.send_json(group_event('[CQ:at,qq=999] 你好'))
                reply = await asyncio.wait_for(sent.get(), 5)
                self.assertEqual(reply['action'], 'send_group_msg')
                self.assertEqual(reply['params']['group_id'], '100')
                await socket.close()
                await asyncio.wait_for(connected.get(), 5)
                bot.stop()
                await asyncio.wait_for(task, 5)
                self.assertFalse(bot._event_tasks)
                self.assertFalse(bot._pending_actions)
            finally:
                bot.stop()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    @unittest.skipUnless(os.name == 'posix', 'Linux SIGTERM subprocess')
    async def test_sigterm_stops_real_bot_and_allows_restart(self):
        connected = asyncio.Queue()
        async def handler(request):
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await connected.put(socket)
            async for _ in socket:
                pass
            return socket
        app = web.Application()
        app.router.add_get('/ws', handler)
        async with TestServer(app) as server, asyncio.timeout(20):
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'config.json'
                path.write_text(json.dumps({
                    'ws_url': str(server.make_url('/ws')).replace('http:', 'ws:'),
                    'allowed_groups': ['100'],
                    'ai': {'base_url': 'https://example.com', 'api_key': 'dummy-key', 'model': 'test-model'},
                }))
                env = {**os.environ, 'BOT_CONFIG': str(path), 'PYTHONUNBUFFERED': '1', 'ONEBOT_WS_URL': '', 'ONEBOT_ACCESS_TOKEN': ''}
                for _ in range(2):
                    process = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve().parents[1] / 'bot.py'), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    try:
                        await connected.get()
                        process.send_signal(signal.SIGTERM)
                        _, output = await asyncio.wait_for(process.communicate(), 5)
                        self.assertEqual(process.returncode, 0)
                        self.assertNotIn(b'Traceback', output)
                    finally:
                        if process.returncode is None:
                            process.kill()
                            await process.wait()
