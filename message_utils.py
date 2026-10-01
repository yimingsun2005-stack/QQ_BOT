from __future__ import annotations

import html
import re
from typing import Any


CQ_CODE_PATTERN = re.compile(r"\[CQ:[^\]]+\]")


def is_bot_mentioned(message: Any, raw_message: str, self_id: str) -> bool:
    """判断 OneBot 11 数组或 CQ 码消息中是否明确 @ 机器人。"""
    if not self_id:
        return False

    if isinstance(message, list):
        for segment in message:
            if not isinstance(segment, dict) or segment.get("type") != "at":
                continue
            data = segment.get("data")
            if isinstance(data, dict) and str(data.get("qq", "")) == self_id:
                return True

    source = message if isinstance(message, str) else raw_message
    mentioned_ids = re.findall(r"\[CQ:at,qq=(\d+)(?:,[^\]]*)?\]", str(source))
    return self_id in mentioned_ids


def extract_mentioned_ids(message: Any, raw_message: str = "") -> list[str]:
    """提取 @ 对象，供上下文标注；调用方不得把原始 QQ 号发送给模型。"""
    if isinstance(message, list):
        ids = [
            str(segment.get("data", {}).get("qq", ""))
            for segment in message
            if isinstance(segment, dict)
            and segment.get("type") == "at"
            and isinstance(segment.get("data"), dict)
        ]
    else:
        ids = re.findall(
            r"\[CQ:at,qq=([^,\]]+)(?:,[^\]]*)?\]",
            message if isinstance(message, str) else raw_message,
        )
    return list(dict.fromkeys(qq for qq in ids if qq))


def has_reply_segment(message: Any, raw_message: str = "") -> bool:
    if isinstance(message, list):
        return any(
            isinstance(segment, dict) and segment.get("type") == "reply"
            for segment in message
        )
    source = message if isinstance(message, str) else raw_message
    return bool(re.search(r"\[CQ:reply(?:,[^\]]*)?\]", source))


def extract_plain_text(
    message: Any, raw_message: str = "", *, normalize_whitespace: bool = True,
) -> str:
    """从 OneBot 消息中提取纯文本，不把 @、图片等 CQ 段发送给 AI。"""
    if isinstance(message, list):
        parts: list[str] = []
        for segment in message:
            if not isinstance(segment, dict) or segment.get("type") != "text":
                continue
            data = segment.get("data")
            if isinstance(data, dict):
                parts.append(str(data.get("text", "")))
        text = "".join(parts)
    elif isinstance(message, str):
        text = message
    else:
        text = raw_message

    text = CQ_CODE_PATTERN.sub(" ", str(text))
    if not normalize_whitespace:
        return html.unescape(text).strip()
    return " ".join(text.split())
