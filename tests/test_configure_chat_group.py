import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from configure_chat_group import add_chat_group


class ConfigureChatGroupTests(unittest.TestCase):
    def test_appends_chat_group_preserving_images_credentials_and_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            original = {
                "allowed_groups": [100, "200"],
                "access_token": "dummy-token",
                "ai": {"api_key": "dummy-key"},
                "novelai_image_generation": {
                    "enabled": True, "allowed_groups": ["100"],
                    "token": "dummy-image-token", "future_option": True,
                },
                "future_setting": {"keep": True},
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            before = path.read_bytes()
            backup = add_chat_group(path, "300")
            self.assertIsNotNone(backup)
            self.assertEqual(backup.read_bytes(), before)
            expected = {**original, "allowed_groups": [100, "200", "300"]}
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), expected)
            self.assertIsNone(add_chat_group(path, "300"))
            self.assertEqual(len(list(path.parent.glob("config.json.bak.*"))), 1)

    def test_existing_integer_group_is_not_rewritten(self):
        for data in ({"allowed_groups": [100]},):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                before = path.read_bytes()
                self.assertIsNone(add_chat_group(path, "100"))
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(list(path.parent.glob("config.json.bak.*")), [])

    def test_first_group_is_added_to_empty_list(self):
        for data in ({"allowed_groups": []}, {}):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                backup = add_chat_group(path, "100")
                self.assertIsNotNone(backup)
                self.assertEqual(json.loads(backup.read_text()), data)
                self.assertEqual(json.loads(path.read_text())["allowed_groups"], ["100"])

    def test_invalid_input_does_not_change_or_backup_config(self):
        for data, group in (
            ({"allowed_groups": ["100"]}, "invalid"),
            ({"allowed_groups": "100"}, "200"),
            ({"allowed_groups": [True]}, "200"),
            ({"allowed_groups": ["invalid"]}, "200"),
            ([], "200"),
        ):
            with self.subTest(data=data, group=group), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                before = path.read_bytes()
                with self.assertRaises(ValueError):
                    add_chat_group(path, group)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(list(path.parent.glob("config.json.bak.*")), [])

    def test_replace_failure_keeps_original_and_backup_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"allowed_groups": ["100"]}', encoding="utf-8")
            before = path.read_bytes()
            with patch("configure_chat_group.Path.replace", side_effect=OSError):
                with self.assertRaises(OSError):
                    add_chat_group(path, "200")
            self.assertEqual(path.read_bytes(), before)
            backups = list(path.parent.glob("config.json.bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), before)
            self.assertEqual(list(path.parent.glob("config.json.tmp.*")), [])


if __name__ == "__main__":
    unittest.main()
