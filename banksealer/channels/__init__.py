"""Transaction channels and the manager that links them to the engine."""
from __future__ import annotations

import queue
from typing import Optional

from ..models import Transaction
from .base import Channel
from .files import CSVChannel, SQLiteChannel
from .mock import MockChannel
from .network import HTTPChannel, TCPChannel

CHANNEL_TYPES: dict[str, type[Channel]] = {
    cls.type_name: cls for cls in (MockChannel, TCPChannel, HTTPChannel, CSVChannel, SQLiteChannel)
}


def create_channel(spec: dict, out_queue: Optional[queue.Queue] = None) -> Channel:
    kind = spec.get("type")
    if kind not in CHANNEL_TYPES:
        raise ValueError(f"unknown channel type {kind!r}; known: {', '.join(CHANNEL_TYPES)}")
    return CHANNEL_TYPES[kind](spec.get("name") or kind, spec.get("options") or {}, spec.get("field_mapping"), out_queue)


class ChannelManager:
    """Owns the channels and turns their raw records into prepared transactions."""

    def __init__(self):
        self.queue: queue.Queue = queue.Queue()
        self.channels: list[Channel] = []
        self.specs: dict[str, dict] = {}

    def add(self, spec: dict) -> Channel:
        names = {c.name for c in self.channels}
        name = spec.get("name") or spec.get("type")
        base, i = name, 2
        while name in names:
            name, i = f"{base} ({i})", i + 1
        spec = {**spec, "name": name}
        channel = create_channel(spec, self.queue)
        self.channels.append(channel)
        self.specs[name] = spec
        return channel

    def remove(self, name: str) -> None:
        channel = self.get(name)
        if channel:
            channel.stop()
            self.channels.remove(channel)
            self.specs.pop(name, None)

    def get(self, name: str) -> Optional[Channel]:
        return next((c for c in self.channels if c.name == name), None)

    def start(self, name: str) -> None:
        channel = self.get(name)
        if channel:
            channel.start()

    def stop(self, name: str) -> None:
        channel = self.get(name)
        if channel:
            channel.stop()

    def start_enabled(self) -> None:
        for channel in self.channels:
            if self.specs.get(channel.name, {}).get("enabled", True):
                channel.start()

    def stop_all(self) -> None:
        for channel in self.channels:
            channel.stop()

    def drain(self, limit: int = 5000) -> tuple[list[Transaction], list[Transaction]]:
        """Return ``(history, live)`` prepared transactions currently waiting in the queue."""
        history, live = [], []
        for _ in range(limit):
            try:
                name, record, is_history = self.queue.get_nowait()
            except queue.Empty:
                break
            channel = self.get(name)
            preparer = channel.preparer if channel else None
            if preparer is None:
                continue
            tx = preparer.prepare(record, name)
            if tx is not None:
                (history if is_history else live).append(tx)
        history.sort(key=lambda t: t.timestamp)
        return history, live

    def specs_list(self) -> list[dict]:
        return [self.specs[c.name] for c in self.channels]


__all__ = ["Channel", "ChannelManager", "CHANNEL_TYPES", "create_channel"]
