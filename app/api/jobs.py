"""Jobs API + SSE streams."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.models import Image, ImageStatus, Job, JobStatus
from app.logging_setup import get_logger
from app.services.comfyui.dispatcher import dispatcher
from app.services.jobs.events import job_event_broadcaster, log_event_broadcaster
from app.services.jobs.queue import queue, queue_summary

router = APIRouter(prefix="/api/jobs", tags=["jobs"])
workers_router = APIRouter(prefix="/api/workers", tags=["workers"])
logs_router = APIRouter(prefix="/api/logs", tags=["logs"])

_log = get_logger("factory.api.jobs")


@router.get("")
async def list_jobs(limit: int = 50, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = (await session.execute(select(Job).order_by(Job.created_at.desc()).limit(limit))).scalars().all()
    return {"count": len(rows), "items": [r.model_dump() for r in rows]}


@router.get("/summary", response_class=HTMLResponse)
async def jobs_summary_inline() -> HTMLResponse:
    """HTMX-friendly summary chip — returns HTML, not JSON."""
    counts = await queue_summary()
    pending = counts.get("pending", 0) + counts.get("running", 0)
    return HTMLResponse(f"queue: {pending} ({counts.get('done', 0)} done)")


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404)
    if job.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
        return job.model_dump()
    job.status = JobStatus.CANCELLED
    img = await session.get(Image, job.image_id)
    if img and img.status in (ImageStatus.QUEUED, ImageStatus.RUNNING):
        img.status = ImageStatus.CANCELLED
    await session.commit()
    return job.model_dump()


@router.post("/{job_id}/retry")
async def retry_job(job_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404)
    job.status = JobStatus.PENDING
    job.error = None
    job.retry_count = 0
    await session.commit()
    await queue.enqueue(job.id, job.priority)
    return job.model_dump()


@router.delete("/{job_id}")
async def delete_job(job_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, bool]:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404)
    await session.delete(job)
    await session.commit()
    return {"ok": True}


@router.get("/stream")
async def jobs_sse(request: Request) -> StreamingResponse:
    q = await job_event_broadcaster.subscribe()

    async def gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                name = ev.pop("_event_name", "message")
                yield f"event: {name}\n"
                yield f"data: {json.dumps(ev)}\n\n"
        finally:
            await job_event_broadcaster.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------- workers


@workers_router.get("")
async def workers_full() -> dict[str, Any]:
    return {"max_parallel_per_worker": dispatcher.max_parallel_per_worker, "workers": dispatcher.summary()}


@workers_router.post("/probe")
async def workers_probe() -> dict[str, Any]:
    """Force a fresh health check on every configured ComfyUI server.

    Useful when a worker was offline at boot and is now reachable, OR when
    the user wants to verify a save without waiting for the 30s periodic probe.
    """
    results = await dispatcher.probe_all()
    healthy_count = sum(1 for r in results.values() if r.get("healthy"))
    _log.info("workers.probe.manual", total=len(results), healthy=healthy_count)
    return {"results": results, "healthy_count": healthy_count, "total": len(results)}


@workers_router.get("/summary", response_class=HTMLResponse)
async def workers_summary_inline() -> HTMLResponse:
    """HTMX-friendly chips."""
    rows = dispatcher.summary()
    if not rows:
        return HTMLResponse('<span class="text-zinc-500">none configured</span>')
    out: list[str] = []
    for w in rows:
        color = "text-green-400" if w["healthy"] else "text-red-400"
        symbol = "●" if w["healthy"] else "○"
        out.append(f'<span class="{color}" title="{w["url"]}">{symbol}{w["in_flight"]}</span>')
    return HTMLResponse(" ".join(out))


# ---------------------------------------------------------------------- logs


@logs_router.get("/stream")
async def logs_sse(request: Request) -> StreamingResponse:
    q = await log_event_broadcaster.subscribe()

    async def gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield "event: log\n"
                yield f"data: {json.dumps(ev, default=str)}\n\n"
        finally:
            await log_event_broadcaster.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# =============================================================================
# Queue pause / start — lets the user review batches before dispatch
# =============================================================================

@router.get("/queue/status")
async def queue_status(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Tell the UI whether auto-dispatch is paused and how many jobs are waiting."""
    from app.services import settings_service
    s_row = await settings_service.get_or_create(session)
    paused = bool(getattr(s_row, "dispatch_paused", False))
    pending_rows = (await session.execute(
        select(Job).where(Job.status == JobStatus.PENDING).order_by(Job.created_at)
    )).scalars().all()
    return {
        "paused": paused,
        "pending_count": len(pending_rows),
        "pending_job_ids": [j.id for j in pending_rows[:200]],
    }


@router.post("/queue/pause")
async def queue_pause(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Stop auto-dispatching newly-committed jobs."""
    from app.services import settings_service
    s_row = await settings_service.get_or_create(session)
    s_row.dispatch_paused = True
    await session.commit()
    _log.info("queue.paused")
    return {"paused": True}


@router.post("/queue/resume")
async def queue_resume(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Re-enable auto-dispatch for future commits (does NOT release pending jobs;
    use /queue/start for that)."""
    from app.services import settings_service
    s_row = await settings_service.get_or_create(session)
    s_row.dispatch_paused = False
    await session.commit()
    _log.info("queue.resumed")
    return {"paused": False}


@router.post("/queue/start")
async def queue_start(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Release every PENDING job onto the dispatcher and unpause future commits.

    This is the button the user clicks after reviewing the Batch Preview / Render
    cards: 'Start Queue'. Idempotent — safe to call repeatedly.
    """
    from app.services import settings_service
    s_row = await settings_service.get_or_create(session)
    s_row.dispatch_paused = False
    await session.commit()
    pending_rows = (await session.execute(
        select(Job).where(Job.status == JobStatus.PENDING).order_by(Job.created_at)
    )).scalars().all()
    released = 0
    for j in pending_rows:
        try:
            await queue.enqueue(j.id, j.priority or 0)
            released += 1
        except Exception as exc:  # noqa: BLE001
            _log.warning("queue.start.enqueue_failed", job_id=j.id, error=str(exc))
    _log.info("queue.started", released=released)
    return {"released": released, "paused": False}


@router.post("/queue/stop")
async def queue_stop(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """HARD STOP — the panic button.

    Does three things atomically:
      1. Set dispatch_paused = True so new commits don't auto-dispatch.
      2. Drain the in-memory queue so no PENDING job gets popped next.
      3. Mark every PENDING Job + QUEUED Image as CANCELLED in the DB.
      4. Best-effort POST /interrupt to every healthy ComfyUI worker so the
         CURRENTLY-RUNNING job aborts mid-step rather than burning compute.
      5. Mark RUNNING images/jobs as CANCELLED so the UI reflects the stop
         (the runner's exception handler will see the cancellation and exit
         cleanly when its loop next checks).

    After Stop the user can hit Start Queue to release any new commits, or
    use Batch retry buttons to put failed/cancelled items back on the queue.
    """
    from sqlalchemy import update as _upd
    from app.services import settings_service
    from app.services.comfyui.dispatcher import dispatcher

    s_row = await settings_service.get_or_create(session)
    s_row.dispatch_paused = True

    drained = await queue.drain()

    # Cancel every PENDING job in the DB.
    res_jobs = await session.execute(
        _upd(Job)
        .where(Job.status.in_([JobStatus.PENDING, JobStatus.RETRYING]))
        .values(status=JobStatus.CANCELLED, error="stopped by user")
    )
    pending_cancelled = res_jobs.rowcount or 0

    # Mark RUNNING jobs CANCELLED too — the runner will fail out cleanly
    # when its WebSocket reads error/abort from the interrupted ComfyUI.
    res_running = await session.execute(
        _upd(Job)
        .where(Job.status == JobStatus.RUNNING)
        .values(status=JobStatus.CANCELLED, error="stopped by user")
    )
    running_cancelled = res_running.rowcount or 0

    # Mirror on Image rows.
    res_imgs = await session.execute(
        _upd(Image)
        .where(Image.status.in_([ImageStatus.QUEUED, ImageStatus.RUNNING]))
        .values(status=ImageStatus.CANCELLED, error="stopped by user")
    )
    images_cancelled = res_imgs.rowcount or 0
    await session.commit()

    # Best-effort interrupt every ComfyUI worker. The dispatcher caches a
    # ComfyUIClient per URL; we resolve via get_client() so the call is fast
    # and doesn't reconnect. Interrupting tells the worker to abort its
    # current step instead of burning compute on a cancelled job.
    interrupted = 0
    try:
        for w in dispatcher.workers:
            try:
                client = dispatcher.get_client(w.url)
                client.interrupt()
                interrupted += 1
            except Exception as exc:  # noqa: BLE001
                _log.warning("queue.stop.worker_interrupt_failed", worker=w.url, error=str(exc))
    except Exception as exc:   # noqa: BLE001
        _log.warning("queue.stop.dispatcher_iter_failed", error=str(exc))

    _log.warning(
        "queue.stopped",
        drained=drained,
        pending_cancelled=pending_cancelled,
        running_cancelled=running_cancelled,
        images_cancelled=images_cancelled,
        workers_interrupted=interrupted,
    )
    return {
        "paused": True,
        "drained": drained,
        "pending_cancelled": pending_cancelled,
        "running_cancelled": running_cancelled,
        "images_cancelled": images_cancelled,
        "workers_interrupted": interrupted,
    }
