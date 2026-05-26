"""Debug & introspection HTTP API.

All operator-facing. Mounted at /api/debug.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.logging_setup import get_logger
from app.services.debug import llm_logger, process_info
from app.services.debug.ring_buffer import error_ring, log_ring, request_ring

router = APIRouter(prefix="/api/debug", tags=["debug"])
_log = get_logger("factory.debug")


@router.get("/status")
def status() -> dict[str, Any]:
    return {
        "process": process_info.snapshot(),
        "ring_buffers": {
            "logs": {"capacity": log_ring.capacity, "size": len(log_ring.snapshot())},
            "errors": {"capacity": error_ring.capacity, "size": len(error_ring.snapshot())},
            "requests": {"capacity": request_ring.capacity, "size": len(request_ring.snapshot())},
        },
    }


@router.get("/echo")
def echo(message: str = "ping") -> dict[str, Any]:
    _log.info("debug.echo", message=message)
    return {"message": message}


@router.get("/logs/recent")
def recent_logs(limit: int = Query(default=200, ge=1, le=2000), level: str | None = Query(default=None)) -> dict[str, Any]:
    rows = log_ring.snapshot(limit=limit, level=level)
    return {"count": len(rows), "items": rows}


@router.get("/errors/recent")
def recent_errors(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    rows = error_ring.snapshot(limit=limit)
    return {"count": len(rows), "items": rows}


@router.get("/requests/recent")
def recent_requests(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    rows = request_ring.snapshot(limit=limit)
    return {"count": len(rows), "items": rows}


@router.get("/llm/calls")
def llm_calls(limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    items = llm_logger.list_recent(limit=limit)
    return {"count": len(items), "items": items}


@router.get("/llm/calls/{call_id}")
def llm_call_detail(call_id: str) -> dict[str, Any]:
    data = llm_logger.read_call(call_id)
    if not data:
        raise HTTPException(status_code=404, detail="LLM call not found")
    return data


@router.post("/test-log")
def test_log() -> dict[str, Any]:
    _log.debug("debug.test_log", level="debug")
    _log.info("debug.test_log", level="info")
    _log.warning("debug.test_log", level="warning")
    _log.error("debug.test_log", level="error")
    return {"emitted": ["debug", "info", "warning", "error"]}


@router.post("/raise")
def deliberately_raise() -> dict[str, Any]:
    msg = "debug.raise - this is a deliberate test exception"
    raise RuntimeError(msg)


@router.post("/client-log")
async def client_log(request: Request) -> dict[str, bool]:
    """Receive a browser-side log event from base.html (window.onerror, unhandledrejection, etc.)."""
    try:
        data = await request.json()
    except Exception:
        try:
            raw = (await request.body()).decode("utf-8", errors="replace")
            data = {"raw": raw}
        except Exception:
            data = {}
    level = str(data.get("level", "info")).lower()
    ua = request.headers.get("user-agent", "")
    fields = {k: v for k, v in data.items() if k != "level"}
    if level == "error":
        _log.error("client.error", user_agent=ua, **fields)
    elif level == "warning":
        _log.warning("client.warn", user_agent=ua, **fields)
    else:
        _log.info("client.log", user_agent=ua, **fields)
    return {"ok": True}


@router.get("/jobs/{job_id}")
async def debug_job(job_id: str, tail: int = 500) -> dict[str, Any]:
    """Full diagnostic dump for one job: DB row + per-job log tail.

    Use this when an image gets stuck or fails — it tells you what the runner
    actually saw and did for that specific job.
    """
    from app.db.engine import get_sessionmaker
    from app.db.models import Image, Job
    from app.services.debug.job_logger import read_job_log

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        img = await session.get(Image, job.image_id) if job.image_id else None
    return {
        "job": {
            "id": job.id,
            "image_id": job.image_id,
            "project_id": job.project_id,
            "status": job.status.value if hasattr(job.status, "value") else job.status,
            "priority": job.priority,
            "worker_url": job.worker_url,
            "prompt_id": job.prompt_id,
            "retry_count": job.retry_count,
            "error": job.error,
            "parameters": job.parameters,
            "result": job.result,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
        },
        "image": (
            {
                "id": img.id, "prompt": img.prompt, "enhanced_prompt": img.enhanced_prompt,
                "status": img.status.value if hasattr(img.status, "value") else img.status,
                "width": img.width, "height": img.height, "seed": img.seed,
                "output_path": img.output_path, "error": img.error,
                "reference_paths": img.reference_paths,
            } if img else None
        ),
        "log": read_job_log(job.project_id, job.id, tail=tail),
    }
