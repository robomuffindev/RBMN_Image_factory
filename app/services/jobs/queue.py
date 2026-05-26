"""In-process priority queue glued to the DB Job table.

The DB row is the source of truth for status; the in-memory queue just
remembers what's eligible to dispatch next. ``recover_running_jobs`` on
startup marks orphaned RUNNING jobs as FAILED so they're not phantom-ly
held.
"""

from __future__ import annotations

import asyncio
import heapq
import time
from typing import NamedTuple

from sqlalchemy import select, update

from app.db.engine import get_sessionmaker
from app.db.models import Image, ImageStatus, Job, JobStatus
from app.logging_setup import get_logger

_log = get_logger("factory.queue")


class QueueItem(NamedTuple):
    neg_priority: int   # higher real priority sorts first via negation
    enqueue_ts: float
    job_id: str


class JobQueue:
    def __init__(self) -> None:
        self._heap: list[QueueItem] = []
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()

    async def enqueue(self, job_id: str, priority: int = 0) -> None:
        async with self._lock:
            heapq.heappush(self._heap, QueueItem(-priority, time.monotonic(), job_id))
        self._wake.set()

    async def pop(self) -> str | None:
        async with self._lock:
            if not self._heap:
                return None
            return heapq.heappop(self._heap).job_id

    async def wait_for_jobs(self, timeout: float = 5.0) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    @property
    def size(self) -> int:
        return len(self._heap)

    async def drain(self) -> int:
        """Remove every pending item from the in-memory queue. Returns count drained.

        The dispatcher will still wake (because of the prior `_wake.set()`),
        find an empty heap, and go back to waiting — no race.
        """
        async with self._lock:
            count = len(self._heap)
            self._heap.clear()
        return count


queue = JobQueue()


async def recover_running_jobs() -> int:
    """Mark stale RUNNING/PENDING jobs as FAILED on startup. Returns count touched."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as session:
        result = await session.execute(
            select(Job).where(Job.status.in_([JobStatus.RUNNING, JobStatus.PENDING, JobStatus.RETRYING]))
        )
        jobs = result.scalars().all()
        for j in jobs:
            j.status = JobStatus.FAILED
            j.error = "interrupted by restart"
        # Mark the corresponding images too.
        for j in jobs:
            img = await session.get(Image, j.image_id)
            if img and img.status in (ImageStatus.RUNNING, ImageStatus.QUEUED):
                img.status = ImageStatus.FAILED
                img.error = "interrupted by restart"
        await session.commit()
    if jobs:
        _log.warning("queue.recover.cleaned_stale", count=len(jobs))
    return len(jobs)


async def repopulate_from_db() -> int:
    """Push any DB jobs still marked PENDING into the in-memory queue."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as session:
        result = await session.execute(
            select(Job).where(Job.status == JobStatus.PENDING).order_by(Job.priority.desc(), Job.created_at.asc())
        )
        pending = result.scalars().all()
        for j in pending:
            await queue.enqueue(j.id, j.priority)
    if pending:
        _log.info("queue.repopulated", count=len(pending))
    return len(pending)


async def queue_summary() -> dict[str, int]:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as session:
        from sqlalchemy import func

        counts: dict[str, int] = {}
        for status in JobStatus:
            r = await session.execute(select(func.count()).select_from(Job).where(Job.status == status))
            counts[status.value] = int(r.scalar_one() or 0)
    counts["in_memory_queue"] = queue.size
    return counts
