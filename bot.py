from __future__ import annotations

import asyncio
import base64
import binascii
import html
import hashlib
import ipaddress
import json
import logging
import math
import os
import random
import re
import secrets
import signal
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

import aiohttp

from ai_client import AIConfig, AIReplyService, AIServiceError, WebSearchConfig
from group_context import GroupContextStore
from message_utils import (
    extract_mentioned_ids,
    extract_plain_text,
    has_reply_segment,
    is_bot_mentioned,
)
from music_service import (
    MusicConfig,
    MusicServiceError,
    NeteaseMusicService,
    build_netease_music_segment,
    parse_music_command,
)
from single_instance import AlreadyRunningError, SingleInstance
from novelai_image_service import (
    NovelAIImageConfig, NovelAIImageService, NovelAIServiceError,
    parse_image_command, validate_prompt,
)
LOGGER = logging.getLogger("qq-bot")
CQ_IMAGE_PATTERN = re.compile(r"\[CQ:image(?:,([^\]]*))?\]")


@dataclass(frozen=True)
class BotConfig:
    ws_url: str
    access_token: str
    allowed_groups: frozenset[str]
    cooldown_seconds: float
    at_sender: bool
    ai: AIConfig = field(default_factory=AIConfig)
    web_search: WebSearchConfig = field(default_factory=WebSearchConfig)
    music: MusicConfig = field(default_factory=MusicConfig)
    auto_reply: AutoReplyConfig = field(default_factory=lambda: AutoReplyConfig())
    novelai_image_generation: NovelAIImageConfig = field(default_factory=NovelAIImageConfig)

    @classmethod
    def load(cls, path: Path) -> "BotConfig":
        from runtime_config import load_bot_config
        return load_bot_config(path)


@dataclass(frozen=True)
class AutoReplyConfig:
    probability: float = 0.1
    min_interval_seconds: float = 30.0
    max_replies_per_hour: int = 100
    context_messages: int = 20
    context_minutes: float = 15.0
    context_max_chars: int = 4000
    context_max_images: int = 3
    enabled: bool = True

    def validate(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("auto_reply.enabled 必须是布尔值")
        if not 0 <= self.probability <= 0.5:
            raise ValueError("auto_reply.probability 必须在 0 到 0.5 之间")
        if not 0 <= self.min_interval_seconds <= 3600:
            raise ValueError("auto_reply.min_interval_seconds 必须在 0 到 3600 之间")
        if not 1 <= self.max_replies_per_hour <= 1000:
            raise ValueError("auto_reply.max_replies_per_hour 必须在 1 到 1000 之间")
        if not 1 <= self.context_messages <= 100:
            raise ValueError("auto_reply.context_messages 必须在 1 到 100 之间")
        if not 1 <= self.context_minutes <= 1440:
            raise ValueError("auto_reply.context_minutes 必须在 1 到 1440 分钟之间")
        if not 100 <= self.context_max_chars <= 20000:
            raise ValueError("auto_reply.context_max_chars 必须在 100 到 20000 之间")
        if not 0 <= self.context_max_images <= 10:
            raise ValueError("auto_reply.context_max_images 必须在 0 到 10 之间")



class AIBot:
    def __init__(
        self,
        config: BotConfig,
        ai_service: AIReplyService | None = None,
        music_service: NeteaseMusicService | None = None,
        image_service: NovelAIImageService | None = None,
    ) -> None:
        self.config = config
        self.ai_service = ai_service or AIReplyService(
            self.config.ai,
            self.config.web_search,
        )
        self.music_service = music_service or NeteaseMusicService(self.config.music)
        self.image_service = image_service or NovelAIImageService(self.config.novelai_image_generation)
        self._image_busy = False
        self._image_task: asyncio.Task[None] | None = None
        self.context = GroupContextStore(
            max_messages=self.config.auto_reply.context_messages,
            max_age_seconds=self.config.auto_reply.context_minutes * 60,
            max_text_chars=self.config.auto_reply.context_max_chars,
            max_images=self.config.auto_reply.context_max_images,
        )
        self._stopping = asyncio.Event()
        self._last_reply_at: dict[tuple[str, str], float] = {}
        self._last_group_reply_at: dict[str, float] = {}
        self._group_reply_times: dict[str, deque[float]] = {}
        self._group_locks: dict[str, asyncio.Lock] = {}
        self._seen_message_ids: dict[str, float] = {}
        self._event_tasks: set[asyncio.Task[None]] = set()
        self._pending_actions: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._pending_outbound: dict[str, str] = {}
        self._bot_message_ids: dict[str, deque[str]] = {}
        self._speaker_salt = secrets.token_bytes(16)

    def _member_label(self, group_id: str, user_id: str) -> str:
        digest = hashlib.blake2s(
            f"{group_id}:{user_id}".encode("utf-8"),
            key=self._speaker_salt,
            digest_size=4,
        ).hexdigest()
        return f"成员#{digest}"

    def stop(self) -> None:
        self._stopping.set()
        if self._image_task is not None:
            self._image_task.cancel()

    async def run(self) -> None:
        reconnect_delay = 1.0
        headers = (
            {"Authorization": f"Bearer {self.config.access_token}"}
            if self.config.access_token
            else {}
        )
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15)

        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            while not self._stopping.is_set():
                try:
                    LOGGER.info("正在连接 OneBot")
                    async with session.ws_connect(
                        self.config.ws_url, heartbeat=30
                    ) as websocket:
                        LOGGER.info("OneBot WebSocket 已连接")
                        reconnect_delay = 1.0
                        consumer = asyncio.create_task(self._consume(websocket))
                        stopper = asyncio.create_task(self._stopping.wait())
                        done, pending = await asyncio.wait(
                            {consumer, stopper},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in pending:
                            task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        if consumer in done:
                            await consumer
                        if self._stopping.is_set():
                            await websocket.close()
                            return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if self._stopping.is_set():
                        break
                    LOGGER.warning(
                        "连接中断（%s）；%.0f 秒后重试", type(exc).__name__, reconnect_delay
                    )
                    try:
                        await asyncio.wait_for(
                            self._stopping.wait(), timeout=reconnect_delay
                        )
                    except asyncio.TimeoutError:
                        pass
                    reconnect_delay = min(reconnect_delay * 2, 30.0)

    async def _consume(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async for frame in websocket:
                if self._stopping.is_set():
                    return
                if frame.type == aiohttp.WSMsgType.TEXT:
                    try:
                        payload = json.loads(frame.data)
                    except json.JSONDecodeError:
                        LOGGER.debug("忽略非 JSON OneBot 数据帧")
                        continue
                    if self._handle_action_response(payload):
                        continue
                    if payload.get("post_type") == "message":
                        if len(self._event_tasks) >= 2048:
                            LOGGER.warning("消息处理队列已满，暂时丢弃新事件")
                            continue
                        task = asyncio.create_task(self._handle_event(websocket, payload))
                        self._event_tasks.add(task)
                        task.add_done_callback(self._event_task_done)
                elif frame.type in {
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.ERROR,
                }:
                    return
        finally:
            tasks = list(self._event_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for future in self._pending_actions.values():
                if not future.done():
                    future.cancel()
            self._pending_actions.clear()

    def _event_task_done(self, task: asyncio.Task[None]) -> None:
        self._event_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            LOGGER.warning("群消息处理失败：%s", type(exc).__name__)

    def _handle_action_response(self, payload: dict[str, Any]) -> bool:
        echo = str(payload.get("echo", ""))
        if not echo:
            return False
        future = self._pending_actions.pop(echo, None)
        if future is not None and not future.done():
            future.set_result(payload)
            return True
        group_id = self._pending_outbound.pop(echo, None)
        if group_id is not None:
            data = payload.get("data")
            message_id = data.get("message_id") if isinstance(data, dict) else None
            if message_id is not None:
                ids = self._bot_message_ids.setdefault(group_id, deque(maxlen=128))
                ids.append(str(message_id))
            return True
        return False

    async def _handle_event(
        self, websocket: aiohttp.ClientWebSocketResponse, event: dict[str, Any]
    ) -> None:
        if event.get("message_type") != "group":
            return

        group_id = str(event.get("group_id", ""))
        user_id = str(event.get("user_id", ""))
        self_id = str(event.get("self_id", ""))
        if not group_id or not user_id or user_id == self_id:
            return
        chat_allowed = group_id in self.config.allowed_groups
        image_allowed = group_id in self.config.novelai_image_generation.allowed_groups
        if not chat_allowed and not image_allowed:
            return

        now = time.monotonic()
        message_id = str(event.get("message_id", ""))
        if message_id:
            dedupe_key = f"{group_id}:{message_id}"
            if dedupe_key in self._seen_message_ids:
                return
            self._seen_message_ids[dedupe_key] = now
            if len(self._seen_message_ids) > 2048:
                cutoff = now - 600
                self._seen_message_ids = {
                    key: seen_at
                    for key, seen_at in self._seen_message_ids.items()
                    if seen_at >= cutoff
                }

        if await self._dispatch_image_command(websocket, event, group_id, user_id, self_id):
            return
        if not chat_allowed:
            return

        lock = self._group_locks.setdefault(group_id, asyncio.Lock())
        async with lock:
            await self._process_group_event(websocket, event, group_id, user_id, self_id)

    async def _dispatch_image_command(
        self, websocket: aiohttp.ClientWebSocketResponse, event: dict[str, Any],
        group_id: str, user_id: str, self_id: str,
    ) -> bool:
        """在群聊天锁之外识别画图指令，避免等待聊天或点歌接口。"""
        if not is_bot_mentioned(event.get("message"), str(event.get("raw_message", "")), self_id):
            return False
        prompt = parse_image_command(extract_plain_text(
            event.get("message"), str(event.get("raw_message", "")),
            normalize_whitespace=False,
        ))
        if prompt is None:
            return False
        if not self.config.novelai_image_generation.enabled:
            await self._send_text_reply(websocket, group_id, user_id, "画图功能未开启")
            return True
        if group_id not in self.config.novelai_image_generation.allowed_groups:
            await self._send_text_reply(websocket, group_id, user_id, "本群未开启画图，请在设置中填写允许画图的群号。")
            return True
        cooldown_key = (group_id, user_id)
        now = time.monotonic()
        remaining = self.config.cooldown_seconds - (
            now - self._last_reply_at.get(cooldown_key, float("-inf"))
        )
        if remaining > 0:
            await self._send_text_reply(
                websocket, group_id, user_id,
                f"请求过于频繁，请在 {max(1, math.ceil(remaining))} 秒后再试。",
            )
        else:
            self._last_reply_at[cooldown_key] = now
            await self._handle_image_request(websocket, group_id, user_id, prompt)
        return True

    async def _process_group_event(
        self,
        websocket: aiohttp.ClientWebSocketResponse,
        event: dict[str, Any],
        group_id: str,
        user_id: str,
        self_id: str,
    ) -> None:
        plain_text = extract_plain_text(
            event.get("message"), str(event.get("raw_message", ""))
        )
        mentioned = is_bot_mentioned(
            event.get("message"), str(event.get("raw_message", "")), self_id
        )
        replied_to_bot = self._is_reply_to_bot(group_id, event)
        quoted = has_reply_segment(
            event.get("message"), str(event.get("raw_message", ""))
        )
        targets = extract_mentioned_ids(
            event.get("message"), str(event.get("raw_message", ""))
        )
        image_data_url = await self._extract_image_data_url(websocket, event)
        if (
            not plain_text
            and not image_data_url
            and not mentioned
            and not quoted
            and not targets
            and not self._contains_image(event)
        ):
            return

        sender = event.get("sender")
        if isinstance(sender, dict):
            nickname = str(sender.get("card") or sender.get("nickname") or "群成员")
        else:
            nickname = "群成员"
        speaker = f"{self._member_label(group_id, user_id)}（{' '.join(nickname.split())[:40]}）"
        addressing_parts = []
        if self_id in targets:
            addressing_parts.append("明确@Bot")
        others = [qq for qq in targets if qq not in (self_id, "all")]
        if others:
            labels = "、".join(self._member_label(group_id, qq) for qq in others[:5])
            addressing_parts.append(f"同时@{labels}")
        if "all" in targets:
            addressing_parts.append("@全体成员")
        if quoted:
            addressing_parts.append(
                "引用Bot的消息" if replied_to_bot else "引用其他消息（对象未确认）"
            )
        addressing = "；".join(addressing_parts)
        if not image_data_url and self._contains_image(event):
            LOGGER.warning("图片读取失败，已改用文字占位")
            plain_text = (plain_text + " [图片暂时无法读取]").strip()
        if not plain_text and not image_data_url:
            plain_text = "（仅@或引用消息，未附文字）"
        self.context.add(
            group_id,
            speaker,
            plain_text,
            image_data_url=image_data_url,
            addressing=addressing,
        )

        cooldown_key = (group_id, user_id)
        now = time.monotonic()
        if now - self._last_reply_at.get(cooldown_key, float("-inf")) < (
            self.config.cooldown_seconds
        ):
            return

        music_query = (
            parse_music_command(plain_text)
            if self.config.music.enabled
            and mentioned
            else None
        )
        if music_query is not None:
            await self._handle_music_request(
                websocket,
                group_id,
                user_id,
                music_query,
            )
            self._last_reply_at[cooldown_key] = now
            return

        if not self._should_reply(group_id, mentioned, now, event):
            return

        self._reserve_group_reply(group_id, now)
        prompt = plain_text[: self.config.ai.max_input_chars]
        context = self.context.recent(group_id, now=now)
        try:
            reply = await self.ai_service.generate_reply(group_id, prompt, context)
        except AIServiceError as exc:
            LOGGER.warning("AI 回复失败（当前消息图片：%s）：%s", bool(image_data_url), type(exc).__name__)
            if not mentioned:
                recent = self._group_reply_times.get(group_id)
                if recent and recent[-1] == now:
                    recent.pop()
                return
            reply = self.config.ai.error_reply
        message: list[dict[str, Any]] = []
        if mentioned and self.config.at_sender:
            message.extend(
                [
                    {"type": "at", "data": {"qq": user_id}},
                    {"type": "text", "data": {"text": " "}},
                ]
            )
        message.append({"type": "text", "data": {"text": reply}})

        echo = str(uuid.uuid4())
        self._pending_outbound[echo] = group_id
        if len(self._pending_outbound) > 1024:
            self._pending_outbound.pop(next(iter(self._pending_outbound)))
        await websocket.send_json(
            {
                "action": "send_group_msg",
                "params": {"group_id": group_id, "message": message},
                "echo": echo,
            }
        )
        reply_time = time.monotonic()
        self._last_reply_at[cooldown_key] = reply_time
        self._last_group_reply_at[group_id] = reply_time
        self.context.add(group_id, "Bot", reply, is_bot=True, now=reply_time)
        LOGGER.info("已发送 AI 回复")

    def _should_reply(
        self,
        group_id: str,
        mentioned: bool,
        now: float,
        event: dict[str, Any] | None = None,
    ) -> bool:
        config = self.config.auto_reply
        if not mentioned and not config.enabled:
            return False
        recent = self._group_reply_times.setdefault(group_id, deque())
        while recent and recent[0] <= now - 3600:
            recent.popleft()
        if len(recent) >= config.max_replies_per_hour:
            return False
        last_reply = self._last_group_reply_at.get(group_id)
        if last_reply is not None and now - last_reply < config.min_interval_seconds:
            return False
        if mentioned:
            return True
        probability = config.probability
        if event and self._is_reply_to_bot(group_id, event):
            probability = 0.5
        return random.random() < probability

    def _is_reply_to_bot(self, group_id: str, event: dict[str, Any]) -> bool:
        message = event.get("message")
        if not isinstance(message, list):
            return False
        bot_ids = self._bot_message_ids.get(group_id, ())
        for segment in message:
            if not isinstance(segment, dict) or segment.get("type") != "reply":
                continue
            data = segment.get("data")
            if isinstance(data, dict) and str(data.get("id", "")) in bot_ids:
                return True
        return False

    def _reserve_group_reply(self, group_id: str, now: float) -> None:
        self._group_reply_times.setdefault(group_id, deque()).append(now)

    async def _extract_image_data_url(
        self,
        websocket: aiohttp.ClientWebSocketResponse,
        event: dict[str, Any],
    ) -> str | None:
        if self.config.auto_reply.context_max_images <= 0:
            return None
        for data in self._image_segments(event):
            for key in ("url", "file"):
                value = data.get(key)
                if isinstance(value, str):
                    result = await self._image_from_value(value)
                    if result:
                        return result
            file_id = str(data.get("file", "") or data.get("file_id", ""))
            if file_id:
                try:
                    response = await self._call_onebot_action(
                        websocket, "get_image", {"file": file_id}
                    )
                    result_data = response.get("data")
                    if isinstance(result_data, dict):
                        for key in ("url", "file"):
                            value = result_data.get(key)
                            if isinstance(value, str):
                                result = await self._image_from_value(value)
                                if result:
                                    return result
                        path = result_data.get("file")
                        if isinstance(path, str):
                            return await self._read_local_image(path)
                except (asyncio.TimeoutError, OSError, ValueError):
                    continue
        return None

    @staticmethod
    def _contains_image(event: dict[str, Any]) -> bool:
        return bool(AIBot._image_segments(event))

    @staticmethod
    def _image_segments(event: dict[str, Any]) -> list[dict[str, Any]]:
        message = event.get("message")
        if isinstance(message, list):
            return [
                segment["data"]
                for segment in message
                if isinstance(segment, dict)
                and segment.get("type") == "image"
                and isinstance(segment.get("data"), dict)
            ]
        source = message if isinstance(message, str) else event.get("raw_message", "")
        if not isinstance(source, str):
            return []
        images = []
        for match in CQ_IMAGE_PATTERN.finditer(source):
            data = {}
            for attribute in (match.group(1) or "").split(","):
                key, separator, value = attribute.partition("=")
                if separator:
                    data[key] = html.unescape(value)
            images.append(data)
        return images

    async def _image_from_value(self, value: str) -> str | None:
        if value.startswith(("https://", "http://")):
            return await self._download_image(value)
        if value.startswith("base64://"):
            encoded = value[len("base64://"):]
        elif value.startswith("data:image/") and ";base64," in value:
            encoded = value.split(";base64,", 1)[1]
        else:
            return None
        if len(encoded) > 4 * 1024 * 1024:
            return None
        try:
            return self._make_image_data_url(base64.b64decode(encoded, validate=True))
        except (ValueError, binascii.Error):
            return None

    async def _call_onebot_action(
        self,
        websocket: aiohttp.ClientWebSocketResponse,
        action: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        echo = str(uuid.uuid4())
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_actions[echo] = future
        try:
            await websocket.send_json(
                {"action": action, "params": params, "echo": echo}
            )
            response = await asyncio.wait_for(future, timeout=10)
        finally:
            self._pending_actions.pop(echo, None)
        if response.get("status") != "ok" or response.get("retcode", 0) != 0:
            raise ValueError("OneBot 接口调用失败")
        return response

    async def _download_image(self, url: str) -> str | None:
        try:
            parsed = urlsplit(url)
            hostname = (parsed.hostname or "").lower()
        except ValueError:
            return None
        if parsed.scheme not in {"http", "https"} or not hostname:
            return None
        if parsed.username or parsed.password:
            return None
        try:
            if not ipaddress.ip_address(hostname).is_global:
                return None
        except ValueError:
            if hostname == "localhost" or hostname.endswith(".localhost"):
                return None
        if parsed.scheme == "http" and not hostname.endswith(
            (".qq.com", ".qpic.cn", ".gtimg.cn")
        ):
            return None
        timeout = aiohttp.ClientTimeout(total=8)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, allow_redirects=False) as response:
                    if response.status != 200:
                        return None
                    if response.content_length and response.content_length > 2 * 1024 * 1024:
                        return None
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        body.extend(chunk)
                        if len(body) > 2 * 1024 * 1024:
                            return None
                    return self._make_image_data_url(bytes(body))
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None

    async def _read_local_image(self, path: str) -> str | None:
        try:
            if path.startswith("file://"):
                parsed = urlsplit(path)
                if parsed.netloc not in {"", "localhost"}:
                    return None
                path = url2pathname(unquote(parsed.path))
            image_path = Path(path)
            if not image_path.is_file() or image_path.stat().st_size > 2 * 1024 * 1024:
                return None
            body = await asyncio.to_thread(image_path.read_bytes)
            return self._make_image_data_url(body)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _make_image_data_url(body: bytes) -> str | None:
        if len(body) > 2 * 1024 * 1024:
            return None
        if body.startswith(b"\xff\xd8\xff"):
            mime_type = "image/jpeg"
        elif body.startswith(b"\x89PNG\r\n\x1a\n"):
            mime_type = "image/png"
        elif body.startswith((b"GIF87a", b"GIF89a")):
            mime_type = "image/gif"
        elif len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
            mime_type = "image/webp"
        else:
            return None
        encoded = base64.b64encode(body).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    async def _handle_image_request(
        self, websocket: aiohttp.ClientWebSocketResponse,
        group_id: str, user_id: str, prompt: str,
    ) -> None:
        if not self.config.novelai_image_generation.enabled:
            await self._send_text_reply(websocket, group_id, user_id, "画图功能未开启")
            return
        try:
            validate_prompt(prompt)
            if not self.config.novelai_image_generation.token.strip():
                raise NovelAIServiceError("请先在设置中填写 NovelAI Token。")
        except NovelAIServiceError as exc:
            await self._send_text_reply(websocket, group_id, user_id, str(exc))
            return
        if self._image_busy:
            await self._send_text_reply(websocket, group_id, user_id, "正在画图，请稍后再试")
            return
        self._image_busy = True
        try:
            await self._send_text_reply(websocket, group_id, user_id, "正在画图")
            task = asyncio.create_task(self._generate_image(websocket, group_id, user_id, prompt))
            self._image_task = task
            self._event_tasks.add(task)
            task.add_done_callback(self._image_task_done)
        except BaseException:
            self._image_busy = False
            raise

    def _image_task_done(self, task: asyncio.Task[None]) -> None:
        if self._image_task is task:
            self._image_task = None
            self._image_busy = False
        self._event_task_done(task)

    async def _generate_image(
        self, websocket: aiohttp.ClientWebSocketResponse,
        group_id: str, user_id: str, prompt: str,
    ) -> None:
        try:
            try:
                image = await self.image_service.generate(prompt)
            except NovelAIServiceError as exc:
                await self._send_text_reply(websocket, group_id, user_id, str(exc))
                return
            except Exception:
                await self._send_text_reply(websocket, group_id, user_id, "画图服务暂时不可用，请稍后再试。")
                return
            message: list[dict[str, Any]] = []
            if self.config.at_sender:
                message.extend([
                    {"type": "at", "data": {"qq": user_id}},
                    {"type": "text", "data": {"text": " "}},
                ])
            message.append({"type": "image", "data": {"file": "base64://" + base64.b64encode(image).decode("ascii")}})
            try:
                response = await self._call_onebot_action(
                    websocket, "send_group_msg", {"group_id": group_id, "message": message},
                )
                if response.get("retcode", 0) != 0:
                    raise ValueError("图片发送失败")
                data = response.get("data")
                if isinstance(data, dict) and data.get("message_id") is not None:
                    self._bot_message_ids.setdefault(group_id, deque(maxlen=128)).append(str(data["message_id"]))
            except (ValueError, asyncio.TimeoutError, aiohttp.ClientError, ConnectionError, RuntimeError):
                await self._send_text_reply(websocket, group_id, user_id, "图片发送失败或未收到确认，请稍后再试。")
        finally:
            self._image_busy = False

    async def _handle_music_request(
        self,
        websocket: aiohttp.ClientWebSocketResponse,
        group_id: str,
        user_id: str,
        query: str,
    ) -> None:
        if not query:
            await self._send_text_reply(
                websocket,
                group_id,
                user_id,
                "请在“点歌”后输入歌曲名，例如：点歌 一个人的北京",
            )
            return

        try:
            track = await self.music_service.search(query)
        except MusicServiceError as exc:
            LOGGER.warning("点歌搜索失败：%s", type(exc).__name__)
            await self._send_text_reply(
                websocket,
                group_id,
                user_id,
                "点歌服务暂时不可用，请稍后再试。",
            )
            return

        if track is None:
            await self._send_text_reply(
                websocket,
                group_id,
                user_id,
                "没有找到这首歌，换个歌名试试吧。",
            )
            return

        try:
            response = await self._call_onebot_action(
                websocket,
                "send_group_msg",
                {"group_id": group_id, "message": [build_netease_music_segment(track)]},
            )
            data = response.get("data")
            message_id = data.get("message_id") if isinstance(data, dict) else None
            if message_id is None:
                raise ValueError("音乐卡片未返回发送确认")
        except (ValueError, asyncio.TimeoutError, aiohttp.ClientError, ConnectionError, RuntimeError) as exc:
            LOGGER.warning("音乐卡片发送未确认：%s", type(exc).__name__)
            await self._send_text_reply(
                websocket,
                group_id,
                user_id,
                "音乐卡片发送失败或未收到确认，请稍后再试。",
            )
            return
        self._bot_message_ids.setdefault(group_id, deque(maxlen=128)).append(str(message_id))
        LOGGER.info("网易云音乐卡片发送已确认")

    async def _send_text_reply(
        self,
        websocket: aiohttp.ClientWebSocketResponse,
        group_id: str,
        user_id: str,
        text: str,
    ) -> None:
        message: list[dict[str, Any]] = []
        if self.config.at_sender:
            message.extend(
                [
                    {"type": "at", "data": {"qq": user_id}},
                    {"type": "text", "data": {"text": " "}},
                ]
            )
        message.append({"type": "text", "data": {"text": text}})
        await self._send_group_message(websocket, group_id, message)

    async def _send_group_message(
        self,
        websocket: aiohttp.ClientWebSocketResponse,
        group_id: str,
        message: list[dict[str, Any]],
    ) -> None:
        echo = str(uuid.uuid4())
        self._pending_outbound[echo] = group_id
        if len(self._pending_outbound) > 1024:
            self._pending_outbound.pop(next(iter(self._pending_outbound)))
        await websocket.send_json(
            {
                "action": "send_group_msg",
                "params": {"group_id": group_id, "message": message},
                "echo": echo,
            }
        )


def resolve_config_path() -> Path:
    return Path(os.getenv("BOT_CONFIG") or Path(__file__).with_name("config.json")).resolve()


async def async_main() -> None:
    config_path = resolve_config_path()
    config = BotConfig.load(config_path)
    bot = AIBot(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, bot.stop)
        except (NotImplementedError, RuntimeError):
            pass
    await bot.run()


def main() -> None:
    logging.basicConfig(
        level={"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
               "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL}.get(
                   os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    try:
        with SingleInstance(resolve_config_path()):
            asyncio.run(async_main())
    except KeyboardInterrupt:
        LOGGER.info("机器人已停止")
    except AlreadyRunningError as exc:
        LOGGER.error("%s", exc)
        raise SystemExit(2) from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        LOGGER.error("启动失败（%s）：请检查配置文件及字段要求", type(exc).__name__)
        raise SystemExit(1) from exc
    except Exception as exc:
        LOGGER.error("运行失败（%s）", type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
