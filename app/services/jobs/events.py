"""Pub/sub event broadcaster for job progress + general log streaming.

Two flavors:
- ``job_event_broadcaster`` — job lifecycle events (started, progress, completed, failed).
- ``log_event_broadcaster`` — every structlog line (used by the debug drawer).

Subscriber semantics:
  Each subscriber gets a per-stream ``asyncio.Queue`` with a generous bound.
  When that bound is exceeded we now drop the OLDEST queued event (not the
  subscriber) so the live page stays subscribed and just loses some
  intermediate progress ticks. The runner is allowed to be faster than the
  browser; we shouldn't punish the user by tearing down their SSE channel
  in the middle of a long batch (the old maxsize=200 behavior dropped the
  whole subscriber on a 176-image batch, forcing a manual page refresh to
  see any status change after that point).
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.logging_setup import get_logger

_log = get_logger("factory.events")


class EventBroadcaster:
    def __init__(self, name: str, queue_max: int = 5000) -> None:
        # 5000 is a deliberately large cap. A 200-image batch with the
        # runner emitting ~30 progress events per job produces ~6000 events,
        # which previously overflowed the 200-cap and dropped the SSE
        # subscriber mid-batch. With drop-oldest semantics the user simply
        # misses some intermediate ticks rather than losing all live updates.
        self.name = name
        self.queue_max = queue_max
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self.queue_max)
        async with self._lock:
            self._subs.add(q)
        await q.put({"type": "stream_ready", "broadcaster": self.name})
        _log.info("events.subscriber_added", broadcaster=self.name, total=len(self._subs))
        return q

    async def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            self._subs.discard(q)
        _log.info("events.subscriber_removed", broadcaster=self.name, total=len(self._subs))

    def put_nowait(self, event: dict[str, Any]) -> None:
        """Push to every subscriber. If a subscriber's queue is full, evict
        the OLDEST event to make room — never drop the subscriber itself.
        Slow clients lose some intermediate progress ticks but keep their
        SSE channel alive and still see start/completed events."""
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop oldest, try again. If even that fails (theoretically
                # impossible since we just emptied a slot), abandon this event
                # for this subscriber and move on — DON'T drop the subscriber.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                    _log.debug("events.queue.evicted_oldest", broadcaster=self.name)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("events.queue.put_failed",
                                 broadcaster=self.name, error=str(exc))

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)


job_event_broadcaster = EventBroadcaster("jobs")
log_event_broadcaster = EventBroadcaster("logs")
