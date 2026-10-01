from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Deque


@dataclass(frozen=True)
class ContextMessage:
    timestamp: float
    speaker: str
    text: str
    image_data_url: str | None = None
    is_bot: bool = False
    addressing: str = ""


class GroupContextStore:
    """Bounded, in-memory recent conversation context, isolated by group."""

    def __init__(
        self,
        *,
        max_messages: int = 20,
        max_age_seconds: float = 900,
        max_text_chars: int = 4000,
        max_images: int = 3,
        max_image_bytes: int = 2 * 1024 * 1024,
        max_total_image_bytes: int = 64 * 1024 * 1024,
        max_groups: int = 256,
    ) -> None:
        self.max_messages = max_messages
        self.max_age_seconds = max_age_seconds
        self.max_text_chars = max_text_chars
        self.max_images = max_images
        self.max_image_bytes = max_image_bytes
        self.max_total_image_bytes = max_total_image_bytes
        self.max_groups = max_groups
        self._groups: dict[str, Deque[ContextMessage]] = {}

    def add(
        self,
        group_id: str,
        speaker: str,
        text: str,
        *,
        image_data_url: str | None = None,
        is_bot: bool = False,
        addressing: str = "",
        now: float | None = None,
    ) -> None:
        timestamp = time.monotonic() if now is None else now
        text = " ".join(text.split())[: self.max_text_chars]
        if image_data_url and self._image_size(image_data_url) > self.max_image_bytes:
            image_data_url = None
        if not text and not image_data_url:
            return

        messages = self._groups.setdefault(group_id, deque())
        self._prune_group(group_id, timestamp)
        messages = self._groups.setdefault(group_id, deque())
        messages.append(
            ContextMessage(timestamp, speaker or "群成员", text, image_data_url, is_bot, addressing)
        )
        while len(messages) > self.max_messages:
            messages.popleft()
        self._prune_group(group_id, timestamp)
        self._trim_groups()
        self._trim_image_cache()

    def recent(self, group_id: str, *, now: float | None = None) -> list[ContextMessage]:
        timestamp = time.monotonic() if now is None else now
        self._prune_group(group_id, timestamp)
        messages = list(self._groups.get(group_id, ()))
        kept: list[ContextMessage] = []
        remaining_chars = self.max_text_chars
        image_count = 0
        for item in reversed(messages):
            if item.image_data_url and image_count >= self.max_images:
                item = ContextMessage(
                    item.timestamp, item.speaker, item.text, None, item.is_bot, item.addressing
                )
            elif item.image_data_url:
                image_count += 1
            if len(item.text) > remaining_chars:
                text = item.text[-remaining_chars:] if remaining_chars else ""
                item = ContextMessage(
                    item.timestamp, item.speaker, text, item.image_data_url, item.is_bot, item.addressing
                )
            remaining_chars -= len(item.text)
            kept.append(item)
        return list(reversed(kept))

    def _prune_group(self, group_id: str, now: float) -> None:
        messages = self._groups.get(group_id)
        if messages is None:
            return
        cutoff = now - self.max_age_seconds
        while messages and messages[0].timestamp < cutoff:
            messages.popleft()
        if not messages:
            self._groups.pop(group_id, None)

    def _trim_image_cache(self) -> None:
        def total_bytes() -> int:
            return sum(
                self._image_size(message.image_data_url)
                for messages in self._groups.values()
                for message in messages
                if message.image_data_url
            )

        while total_bytes() > self.max_total_image_bytes:
            oldest_group: str | None = None
            oldest_index: int | None = None
            oldest_time = float("inf")
            for group_id, messages in self._groups.items():
                for index, message in enumerate(messages):
                    if message.image_data_url and message.timestamp < oldest_time:
                        oldest_group = group_id
                        oldest_index = index
                        oldest_time = message.timestamp
            if oldest_group is None or oldest_index is None:
                return
            messages = self._groups[oldest_group]
            items = list(messages)
            old = items[oldest_index]
            items[oldest_index] = ContextMessage(
                old.timestamp, old.speaker, old.text, None, old.is_bot, old.addressing
            )
            self._groups[oldest_group] = deque(items)

    def _trim_groups(self) -> None:
        while len(self._groups) > self.max_groups:
            oldest_group = min(
                self._groups,
                key=lambda group_id: (
                    self._groups[group_id][-1].timestamp
                    if self._groups[group_id]
                    else float("-inf")
                ),
            )
            self._groups.pop(oldest_group, None)

    @staticmethod
    def _image_size(image_data_url: str | None) -> int:
        if not image_data_url:
            return 0
        try:
            encoded = image_data_url.split(",", 1)[1]
            return len(encoded) * 3 // 4
        except (IndexError, ValueError):
            return len(image_data_url)
