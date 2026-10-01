import sys
import types
import unittest

try:
    import aiohttp  # noqa: F401
except ModuleNotFoundError:
    sys.modules["aiohttp"] = types.SimpleNamespace()

from music_service import (
    MusicConfig,
    MusicServiceError,
    MusicTrack,
    NeteaseMusicService,
    build_netease_music_segment,
    parse_music_command,
)


class MusicCommandTests(unittest.TestCase):
    def test_parses_command_with_or_without_space(self) -> None:
        self.assertEqual(parse_music_command("点歌一个人的北京"), "一个人的北京")
        self.assertEqual(parse_music_command("点歌 一个人的北京"), "一个人的北京")

    def test_normalizes_query_whitespace(self) -> None:
        self.assertEqual(parse_music_command("  点歌  一个人的  北京  "), "一个人的 北京")

    def test_distinguishes_missing_title_and_non_command(self) -> None:
        self.assertEqual(parse_music_command("点歌"), "")
        self.assertIsNone(parse_music_command("请帮我点歌"))


class NeteaseMusicServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = NeteaseMusicService(MusicConfig())

    def test_parses_first_search_result(self) -> None:
        track = self.service._parse_first_track(
            {
                "result": {
                    "songs": [
                        {
                            "id": 123456,
                            "name": "一个人的北京",
                            "artists": [{"name": "歌手甲"}, {"name": "歌手乙"}],
                        }
                    ]
                }
            }
        )
        self.assertEqual(
            track,
            MusicTrack(
                song_id="123456",
                title="一个人的北京",
                artists="歌手甲 / 歌手乙",
            ),
        )

    def test_empty_search_result_returns_none(self) -> None:
        self.assertIsNone(self.service._parse_first_track({"result": {"songs": []}}))
        self.assertIsNone(self.service._parse_first_track({"code": 200, "result": {"songCount": 0}}))

    def test_search_errors_are_not_treated_as_no_matching_song(self) -> None:
        responses = [
            {"code": 200, "result": None},
            {"code": 200},
            {"code": 200, "result": {}},
            {"code": 200, "result": {"songs": {}}},
            {"code": -460, "result": {"songs": []}, "message": "private-provider-error"},
        ]
        for response in responses:
            with self.subTest(response=response):
                with self.assertRaises(MusicServiceError) as caught:
                    self.service._parse_first_track(response)
                self.assertNotIn("private-provider-error", str(caught.exception))

    def test_invalid_song_id_is_rejected(self) -> None:
        with self.assertRaises(MusicServiceError):
            self.service._parse_first_track(
                {"result": {"songs": [{"id": "not-a-number", "name": "测试"}]}}
            )

    def test_builds_netease_music_segment(self) -> None:
        segment = build_netease_music_segment(
            MusicTrack(song_id="123456", title="测试", artists="歌手")
        )
        self.assertEqual(
            segment,
            {"type": "music", "data": {"type": "163", "id": "123456"}},
        )


if __name__ == "__main__":
    unittest.main()
