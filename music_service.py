from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

import aiohttp


class MusicServiceError(RuntimeError):
    """网易云音乐搜索请求失败或响应格式无效。"""


@dataclass(frozen=True)
class MusicConfig:
    enabled: bool = True
    search_url: str = "https://music.163.com/api/search/get"
    timeout_seconds: float = 10.0
    max_query_chars: int = 100

    def validate(self) -> None:
        if not self.search_url.startswith(("http://", "https://")):
            raise ValueError("music.search_url 必须以 http:// 或 https:// 开头")
        if not 1 <= self.timeout_seconds <= 60:
            raise ValueError("music.timeout_seconds 必须在 1 到 60 之间")
        if not 1 <= self.max_query_chars <= 200:
            raise ValueError("music.max_query_chars 必须在 1 到 200 之间")


@dataclass(frozen=True)
class MusicTrack:
    song_id: str
    title: str
    artists: str


def parse_music_command(text: str) -> str | None:
    """解析以“点歌”开头的指令；空字符串表示指令缺少歌名。"""

    match = re.fullmatch(r"\s*点歌\s*(.*?)\s*", text, flags=re.DOTALL)
    if match is None:
        return None
    return re.sub(r"\s+", " ", match.group(1)).strip()


def build_netease_music_segment(track: MusicTrack) -> dict[str, Any]:
    return {
        "type": "music",
        "data": {
            "type": "163",
            "id": track.song_id,
        },
    }


class NeteaseMusicService:
    """通过网易云音乐搜索接口获取歌曲 ID。"""

    def __init__(self, config: MusicConfig) -> None:
        config.validate()
        self.config = config

    async def search(self, query: str) -> MusicTrack | None:
        normalized_query = re.sub(r"\s+", " ", query).strip()
        if not normalized_query:
            raise MusicServiceError("歌曲名不能为空")
        if len(normalized_query) > self.config.max_query_chars:
            raise MusicServiceError(
                f"歌曲名不能超过 {self.config.max_query_chars} 个字符"
            )

        data = await self._request_search(normalized_query)
        return self._parse_first_track(data)

    async def _request_search(self, query: str) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
        headers = {
            "Referer": "https://music.163.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
            ),
        }
        payload = {
            "s": query,
            "type": "1",
            "limit": "1",
            "offset": "0",
            "total": "true",
            "csrf_token": "",
        }

        try:
            async with aiohttp.ClientSession(
                headers=headers,
                timeout=timeout,
            ) as session:
                async with session.post(
                    self.config.search_url,
                    data=payload,
                ) as response:
                    if response.status < 200 or response.status >= 300:
                        raise MusicServiceError(
                            f"网易云音乐搜索接口返回 HTTP {response.status}"
                        )
                    data = await response.json(content_type=None)
        except MusicServiceError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise MusicServiceError("无法连接网易云音乐搜索服务") from exc
        except ValueError as exc:
            raise MusicServiceError("网易云音乐搜索接口返回的不是有效 JSON") from exc

        if not isinstance(data, dict):
            raise MusicServiceError("网易云音乐搜索接口响应格式无效")
        return data

    @staticmethod
    def _parse_first_track(data: dict[str, Any]) -> MusicTrack | None:
        if data.get("code", 200) not in (200, "200"):
            raise MusicServiceError("网易云音乐搜索接口未正常返回结果")
        result = data.get("result")
        if not isinstance(result, dict):
            raise MusicServiceError("网易云音乐搜索接口缺少结果数据")
        songs = result.get("songs")
        if songs is None and result.get("songCount") == 0:
            return None
        if not isinstance(songs, list):
            raise MusicServiceError("网易云音乐搜索接口歌曲列表格式无效")
        if not songs:
            return None

        song = songs[0]
        if not isinstance(song, dict):
            raise MusicServiceError("网易云音乐歌曲数据格式无效")
        song_id = str(song.get("id", "")).strip()
        title = str(song.get("name", "")).strip()
        if not song_id or not song_id.isascii() or not song_id.isdigit():
            raise MusicServiceError("网易云音乐歌曲 ID 无效")

        artists_data = song.get("artists", song.get("ar", []))
        artist_names: list[str] = []
        if isinstance(artists_data, list):
            artist_names = [
                str(artist.get("name", "")).strip()
                for artist in artists_data
                if isinstance(artist, dict) and str(artist.get("name", "")).strip()
            ]

        return MusicTrack(
            song_id=song_id,
            title=title,
            artists=" / ".join(artist_names),
        )
