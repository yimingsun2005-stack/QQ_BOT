from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ReplyLimitsConfig:
    min_interval_seconds: float = 30.0
    max_replies_per_hour: int = 100

    def validate(self) -> None:
        if not math.isfinite(self.min_interval_seconds) or not 0 <= self.min_interval_seconds <= 3600:
            raise ValueError("群回复间隔必须在 0 到 3600 秒之间")
        if not 1 <= self.max_replies_per_hour <= 1000:
            raise ValueError("每小时最多回复次数必须在 1 到 1000 之间")


@dataclass(frozen=True)
class ContextConfig:
    context_messages: int = 20
    context_minutes: float = 15.0
    context_max_chars: int = 4000
    context_max_images: int = 3

    def validate(self) -> None:
        if not 1 <= self.context_messages <= 100:
            raise ValueError("群聊上下文消息数必须在 1 到 100 之间")
        if not math.isfinite(self.context_minutes) or not 1 <= self.context_minutes <= 1440:
            raise ValueError("群聊上下文时长必须在 1 到 1440 分钟之间")
        if not 100 <= self.context_max_chars <= 20000:
            raise ValueError("群聊上下文字符数必须在 100 到 20000 之间")
        if not 0 <= self.context_max_images <= 10:
            raise ValueError("群聊上下文图片数必须在 0 到 10 之间")


def migrate_chat_config(data: dict[str, Any]) -> dict[str, Any]:
    """Move legacy context/limits without retaining unsolicited-chat controls."""
    legacy = data.get("auto_reply", {})
    context = data.get("context", {})
    limits = data.get("reply_limits", {})
    for name, value in (("auto_reply", legacy), ("context", context), ("reply_limits", limits)):
        if not isinstance(value, dict):
            raise ValueError(f"{name} 配置必须是 JSON 对象")
    context_values = asdict(ContextConfig())
    limit_values = asdict(ReplyLimitsConfig())
    retired = {"enabled", "probability", "engagement_window_seconds", "engagement_bonus"}
    # Unknown legacy fields remain available; known chat controls are discarded.
    context_values.update({key: value for key, value in legacy.items()
                           if key not in retired and key not in limit_values})
    limit_values.update({key: legacy[key] for key in limit_values if key in legacy})
    context_values.update(context)
    limit_values.update(limits)
    migrated = dict(data)
    migrated.pop("auto_reply", None)
    migrated["context"] = context_values
    migrated["reply_limits"] = limit_values
    return migrated


def read_chat_config(data: dict[str, Any]) -> tuple[ContextConfig, ReplyLimitsConfig]:
    migrated = migrate_chat_config(data)
    context_data = migrated["context"]
    limits_data = migrated["reply_limits"]
    context = ContextConfig(
        context_messages=int(context_data["context_messages"]),
        context_minutes=float(context_data["context_minutes"]),
        context_max_chars=int(context_data["context_max_chars"]),
        context_max_images=int(context_data["context_max_images"]),
    )
    limits = ReplyLimitsConfig(
        min_interval_seconds=float(limits_data["min_interval_seconds"]),
        max_replies_per_hour=int(limits_data["max_replies_per_hour"]),
    )
    context.validate()
    limits.validate()
    return context, limits
