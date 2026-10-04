"""Validate configuration fields without writing or exposing their values."""
from __future__ import annotations

import math
from typing import Any


class ConfigurationError(ValueError):
    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"配置字段无效或缺失：{field}")


def normalize_config(data: Any, defaults: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ConfigurationError(prefix or "config")
    result = {}
    for key, default in defaults.items():
        field = f"{prefix}.{key}" if prefix else key
        value = data.get(key)
        if isinstance(default, dict):
            result[key] = normalize_config({} if value is None else value, default, field)
            continue
        if value is None or (value == "" and isinstance(default, str)):
            value = default
        if isinstance(default, bool):
            valid = type(value) is bool
        elif isinstance(default, (int, float)):
            valid = type(value) in (int, float) and math.isfinite(value)
            if type(default) is int:
                valid = valid and type(value) is int
        elif isinstance(default, list):
            valid = isinstance(value, list) and all(
                isinstance(item, str) and item.isascii() and item.isdigit() for item in value
            )
        else:
            valid = isinstance(value, str)
        if not valid:
            raise ConfigurationError(field)
        result[key] = value.strip() if isinstance(value, str) else value
    return result
