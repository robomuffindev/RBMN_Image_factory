import asyncio

import pytest

from app.services.jobs.queue import JobQueue


@pytest.mark.asyncio
async def test_priority_ordering():
    q = JobQueue()
    await q.enqueue("low", priority=0)
    await q.enqueue("high", priority=10)
    await q.enqueue("mid", priority=5)
    assert await q.pop() == "high"
    assert await q.pop() == "mid"
    assert await q.pop() == "low"
    assert await q.pop() is None


@pytest.mark.asyncio
async def test_wake():
    q = JobQueue()

    async def producer():
        await asyncio.sleep(0.05)
        await q.enqueue("a", 0)

    asyncio.create_task(producer())
    await q.wait_for_jobs(timeout=1.0)
    assert await q.pop() == "a"
