"""
Thread-safe log broadcaster for the SSE dashboard feed.

Hooks into Python's logging framework and fans out log records to all
active SSE subscribers without blocking the async event loop.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any


class LogBroadcaster(logging.Handler):
    """Async-safe logging.Handler that multicasts records to SSE queues."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self.setFormatter(logging.Formatter("%(message)s"))

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=300)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def emit(self, record: logging.LogRecord) -> None:
        if not self._subscribers or self._loop is None:
            return

        # Only surface src.* and config.* logs — suppress noisy internals
        if not any(record.name.startswith(p) for p in ("src.", "config.", "__main__")):
            return

        entry: dict[str, Any] = {
            "level": record.levelname,
            "name": record.name.split(".")[-1],
            "msg": record.getMessage(),
            "ts": record.created,
        }

        def _fan_out() -> None:
            for q in self._subscribers[:]:
                try:
                    q.put_nowait(entry)
                except asyncio.QueueFull:
                    pass

        try:
            self._loop.call_soon_threadsafe(_fan_out)
        except RuntimeError:
            pass


log_broadcaster = LogBroadcaster()
