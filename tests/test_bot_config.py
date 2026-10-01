import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import BotConfig


def write_config(directory: str, **overrides) -> Path:
    data = {
        "ws_url": "ws://localhost:4321",
        "access_token": "",
        "allowed_groups": ["100"],
    }
    data.update(overrides)
    if isinstance(data.get('ai'), dict):
        data['ai'] = {'base_url': 'https://example.com', 'model': 'test-model', **data['ai']}
    path = Path(directory) / "config.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


class BotConfigTests(unittest.TestCase):
    def test_requires_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory)
            with patch.dict(
                os.environ,
                {"AI_API_KEY": "", "DEEPSEEK_API_KEY": ""},
                clear=False,
            ):
                with self.assertRaises(ValueError):
                    BotConfig.load(path)

    def test_reads_environment_key_and_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(
                directory,
                ai={
                    "model": "custom-model",
                    "history_turns": 3,
                    "max_input_chars": 800,
                },
                auto_reply={
                    "enabled": False,
                    "probability": 0.22,
                    "context_messages": 12,
                    "engagement_window_seconds": "ignored-old-value",
                    "engagement_bonus": "ignored-old-value",
                },
            )
            with patch.dict(os.environ, {"AI_API_KEY": "test-key"}, clear=False):
                config = BotConfig.load(path)
        self.assertEqual(config.ai.api_key, "test-key")
        self.assertEqual(config.ai.model, "custom-model")
        self.assertEqual(config.ai.max_input_chars, 800)
        self.assertEqual(config.auto_reply.probability, 0.22)
        self.assertEqual(config.auto_reply.context_messages, 12)
        self.assertFalse(config.auto_reply.enabled)

    def test_config_key_takes_precedence_over_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(
                directory,
                ai={"api_key": "saved-key"},
            )
            with patch.dict(os.environ, {"AI_API_KEY": "environment-key"}, clear=False):
                config = BotConfig.load(path)
        self.assertEqual(config.ai.api_key, "saved-key")

    def test_reads_music_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(
                directory,
                ai={"api_key": "saved-key"},
                music={
                    "enabled": False,
                    "search_url": "https://example.com/music-search",
                    "timeout_seconds": 8,
                    "max_query_chars": 80,
                },
            )
            config = BotConfig.load(path)
        self.assertFalse(config.music.enabled)
        self.assertEqual(
            config.music.search_url,
            "https://example.com/music-search",
        )
        self.assertEqual(config.music.timeout_seconds, 8)
        self.assertEqual(config.music.max_query_chars, 80)

    def test_reads_web_search_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(
                directory,
                ai={"api_key": "saved-key"},
                web_search={
                    "enabled": False,
                },
            )
            config = BotConfig.load(path)
        self.assertFalse(config.web_search.enabled)

    def test_at_sender_defaults_to_false(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, ai={"api_key": "saved-key"})
            config = BotConfig.load(path)
        self.assertFalse(config.at_sender)

    def test_explicit_at_sender_false_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(
                directory,
                ai={"api_key": "saved-key"},
                at_sender=False,
            )
            config = BotConfig.load(path)
        self.assertFalse(config.at_sender)


if __name__ == "__main__":
    unittest.main()
