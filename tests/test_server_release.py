import asyncio
import copy
import io
import json
import logging
import os
import tempfile
import unittest
import zipfile
import tarfile
from pathlib import Path
from unittest.mock import patch

from bot import AIBot, BotConfig
from ai_client import AIServiceError
from tools.build_release import FILES, ROOT, assert_blank, build, release_contents
from test_bot import FakeAIService, FakeWebSocket, group_event, make_config


class ServerConfigTests(unittest.TestCase):
    def blank(self):
        return json.loads((ROOT / 'config.example.json').read_text())

    def configured(self):
        data = self.blank()
        data.update(ws_url='ws://localhost:3001', allowed_groups=['100'])
        data['ai'].update(base_url='https://example.com', model='test-model', api_key='dummy-key')
        return data

    def load(self, data):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps(data), encoding='utf-8')
            before = path.read_bytes()
            with patch.dict(os.environ, {}, clear=True):
                result = BotConfig.load(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.iterdir()), [path])
            return result

    def test_template_preserves_every_field_with_only_blank_values(self):
        assert_blank(self.blank())
        self.assertIn('timeout_seconds', self.blank()['ai'])
        self.assertIn('token', self.blank()['novelai_image_generation'])

    def test_unconfigured_template_is_rejected_without_values_in_error(self):
        with self.assertRaises(ValueError):
            self.load(self.blank())

    def test_null_values_use_runtime_defaults_without_writing_back(self):
        config = self.load(self.configured())
        self.assertEqual(config.cooldown_seconds, 5)
        self.assertEqual(config.ai.max_tokens, 500)
        self.assertEqual(config.novelai_image_generation.width, 832)
        self.assertFalse(config.at_sender)
        self.assertFalse(config.auto_reply.enabled)
        self.assertFalse(config.music.enabled)
        self.assertFalse(config.web_search.enabled)
        self.assertFalse(config.novelai_image_generation.enabled)

    def test_required_fields_and_numeric_types_are_checked(self):
        for section, key, value in [
            (None, 'allowed_groups', []), (None, 'allowed_groups', [100]),
            (None, 'cooldown_seconds', float('nan')), (None, 'cooldown_seconds', True),
            ('ai', 'model', ''), ('ai', 'base_url', ''), ('ai', 'api_key', ''),
            ('ai', 'max_tokens', 1.5), ('music', 'enabled', 'false'),
            ('auto_reply', 'enabled', 1),
        ]:
            with self.subTest(section=section, key=key):
                data = self.configured()
                (data if section is None else data[section])[key] = value
                with self.assertRaises(ValueError):
                    self.load(data)

    def test_image_enable_requires_its_own_credentials_and_groups(self):
        for token, allowed in [('', []), ('dummy-token', []), ('', ['100'])]:
            data = self.configured()
            data['novelai_image_generation'].update(enabled=True, token=token, allowed_groups=allowed)
            with self.assertRaises(ValueError):
                self.load(data)
        data['novelai_image_generation'].update(token='dummy-token', allowed_groups=['200'])
        self.assertEqual(self.load(data).novelai_image_generation.allowed_groups, ['200'])

    def test_archive_does_not_read_local_config_or_include_extraneous_files(self):
        marker = 'synthetic-' + 'private-marker'
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'source'
            root.mkdir()
            for name in FILES:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / name).read_bytes())
            for name in ('config.json', 'config.json.bak', 'private.log', '.bot.pid', 'desktop_app.pyw'):
                (root / name).write_text(marker)
            archives = build(Path(folder) / 'output', root)
            contents = release_contents(root)
            with zipfile.ZipFile(archives[0]) as archive:
                self.assertEqual(set(archive.namelist()), {'QQ_BOT/' + name for name in contents})
                for name, data in contents.items():
                    self.assertEqual(archive.read('QQ_BOT/' + name), data)
                assert_blank(json.loads(archive.read('QQ_BOT/config.json')))
            with tarfile.open(archives[1]) as archive:
                self.assertEqual(set(archive.getnames()), {'QQ_BOT/' + name for name in contents})
                for member in archive.getmembers():
                    self.assertEqual((member.uid, member.gid, member.uname, member.gname), (0, 0, '', ''))
                    self.assertEqual(archive.extractfile(member).read(), contents[member.name.removeprefix('QQ_BOT/')])
            for data in contents.values():
                self.assertNotIn(marker.encode(), data)


class PrivacyAndWhitelistTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_chat_whitelist_never_calls_ai_or_collects_context(self):
        ai = FakeAIService()
        bot = AIBot(make_config(allowed_groups=frozenset()), ai_service=ai)
        socket = FakeWebSocket()
        await bot._handle_event(socket, group_event('[CQ:at,qq=999] 你好'))
        self.assertFalse(ai.calls)
        self.assertFalse(socket.sent)
        self.assertEqual(bot.context.recent('100'), [])

    async def test_logs_do_not_include_message_group_or_exception_values(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger('qq-bot')
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        try:
            ai = FakeAIService(error=AIServiceError('synthetic-secret-exception'))
            bot = AIBot(make_config(), ai_service=ai)
            event = group_event('[CQ:at,qq=999] 你好')
            await bot._handle_event(FakeWebSocket(), event)
            output = stream.getvalue()
            self.assertIn('AIServiceError', output)
            self.assertNotIn('synthetic-secret-exception', output)
            self.assertNotIn(str(event['group_id']), output)
            self.assertNotIn(str(event['user_id']), output)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
