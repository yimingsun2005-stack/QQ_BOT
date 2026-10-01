"""Read server configuration without modifying it or exposing values in errors."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from urllib.parse import urlsplit

from ai_client import AIConfig, WebSearchConfig
from music_service import MusicConfig
from novelai_image_service import NovelAIImageConfig


def object_value(data: dict, key: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{key} 必须是 JSON 对象")
    return value


def text_value(data: dict, key: str, default: str = "") -> str:
    value = data.get(key)
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是字符串")
    return value.strip()


def switch(data: dict, key: str) -> bool:
    value = data.get(key)
    if value is None:
        return False
    if type(value) is not bool:
        raise ValueError(f"{key} 必须是布尔值或 null")
    return value


def number(data: dict, key: str, default: int | float) -> int | float:
    value = data.get(key)
    if value is None:
        return default
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{key} 必须是有限数字或 null")
    if type(default) is int and type(value) is not int:
        raise ValueError(f"{key} 必须是整数或 null")
    return value


def groups(data: dict, key: str = "allowed_groups") -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.isascii() or not item.isdigit()
        for item in value
    ):
        raise ValueError(f"{key} 必须是数字字符串数组")
    return list(dict.fromkeys(value))


def load_bot_config(path: Path):
    from bot import AutoReplyConfig, BotConfig

    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("配置必须是 JSON 对象")
    ws_url = (os.getenv("ONEBOT_WS_URL") or text_value(data, "ws_url")).strip()
    try:
        parsed = urlsplit(ws_url)
        valid_ws = parsed.scheme in ("ws", "wss") and bool(parsed.hostname) and parsed.username is None
        parsed.port
    except ValueError:
        valid_ws = False
    if not valid_ws:
        raise ValueError("ws_url 必须是有效的 ws:// 或 wss:// 地址")
    token = (os.getenv("ONEBOT_ACCESS_TOKEN") or text_value(data, "access_token")).strip()
    if any(c in token for c in "\r\n"):
        raise ValueError("access_token 格式无效")
    allowed_groups = groups(data)
    if not allowed_groups:
        raise ValueError("allowed_groups 至少填写一个聊天群")
    cooldown = number(data, "cooldown_seconds", 5.0)
    if cooldown < 0:
        raise ValueError("cooldown_seconds 不能小于 0")

    ai_data = object_value(data, "ai")
    ai = AIConfig(
        base_url=text_value(ai_data, "base_url"),
        api_key=(text_value(ai_data, "api_key") or os.getenv("AI_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or "").strip(),
        model=text_value(ai_data, "model"),
        system_prompt=text_value(ai_data, "system_prompt", AIConfig.system_prompt),
        timeout_seconds=number(ai_data, "timeout_seconds", 60.0),
        max_tokens=number(ai_data, "max_tokens", 500),
        max_input_chars=number(ai_data, "max_input_chars", 2000),
        max_reply_chars=number(ai_data, "max_reply_chars", 1000),
        error_reply=text_value(ai_data, "error_reply", AIConfig.error_reply),
    )
    ai.validate()
    if any(c in ai.api_key for c in "\r\n"):
        raise ValueError("ai.api_key 格式无效")
    search = WebSearchConfig(enabled=switch(object_value(data, "web_search"), "enabled"))
    if search.enabled and ai.base_url.rstrip("/") != "https://api.deepseek.com":
        raise ValueError("web_search.enabled 要求 DeepSeek 官方 AI 地址")
    music_data = object_value(data, "music")
    music = MusicConfig(
        enabled=switch(music_data, "enabled"),
        search_url=text_value(music_data, "search_url", MusicConfig.search_url),
        timeout_seconds=number(music_data, "timeout_seconds", 10.0),
        max_query_chars=number(music_data, "max_query_chars", 100),
    )
    music.validate()
    auto_data = object_value(data, "auto_reply")
    defaults = AutoReplyConfig()
    auto = AutoReplyConfig(**{
        key: switch(auto_data, key) if key == "enabled" else number(auto_data, key, getattr(defaults, key))
        for key in defaults.__dataclass_fields__
    })
    auto.validate()

    image_data = object_value(data, "novelai_image_generation")
    image_defaults = NovelAIImageConfig()
    image = NovelAIImageConfig(
        enabled=switch(image_data, "enabled"),
        token=text_value(image_data, "token"),
        allowed_groups=groups(image_data),
        width=number(image_data, "width", image_defaults.width),
        height=number(image_data, "height", image_defaults.height),
        steps=number(image_data, "steps", image_defaults.steps),
        guidance=number(image_data, "guidance", image_defaults.guidance),
        sampler=text_value(image_data, "sampler", image_defaults.sampler),
        timeout_seconds=number(image_data, "timeout_seconds", image_defaults.timeout_seconds),
    )
    image.validate()
    if image.enabled and (not image.token or not image.allowed_groups):
        raise ValueError("novelai_image_generation 启用时必须填写 token 和 allowed_groups")
    return BotConfig(
        ws_url=ws_url, access_token=token, allowed_groups=frozenset(allowed_groups),
        cooldown_seconds=cooldown, at_sender=switch(data, "at_sender"), ai=ai,
        web_search=search, music=music, auto_reply=auto, novelai_image_generation=image,
    )
