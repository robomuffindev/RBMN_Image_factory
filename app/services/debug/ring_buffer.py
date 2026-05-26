"""Thread-safe in-memory ring buffer of recent log lines.

The debug drawer in the UI streams new lines via SSE, but on first connect
we also want to show *what just happened*. This buffer is the source of
truth for "the last N log records" and is queried by the debug API.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any


class RingBuffer:
    """Bounded deque with a lock — concurrent producers, snapshot reads."""

    def __init__(self, capacity: int = 2000) -> None:
        self._capacity = capacity
        self._buf: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def append(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._buf.append(record)

    def snapshot(self, limit: int | None = None, level: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._buf)
        if level:
            wanted = level.upper()
            items = [r for r in items if str(r.get("level", "")).upper() == wanted]
        if limit is not None and limit > 0:
            items = items[-limit:]
        return items

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()

    @property
    def capacity(self) -> int:
        return self._capacity


# Process-wide singletons.
log_ring = RingBuffer(capacity=2000)        # generic structlog records
error_ring = RingBuffer(capacity=200)       # errors + tracebacks only
request_ring = RingBuffer(capacity=500)     # HTTP request/response summaries
