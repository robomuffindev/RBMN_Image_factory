"""HTTP request logging middleware.

Every incoming request gets:
- A unique ``X-Request-ID`` (echoed back in the response).
- A structlog log line on completion with method, path, status, duration.
- A summary record in the ``request_ring`` buffer for the debug UI.
- A bound contextvar so any logs emitted *during* the request carry the
  request_id automatically.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.logging_setup import bind_context, clear_context, get_logger
from app.services.debug.ring_buffer import request_ring

_log = get_logger("factory.http")


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        # Reuse upstream request ID if a proxy passed one; otherwise mint our own.
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        bind_context(request_id=rid, path=request.url.path, method=request.method)
        t0 = time.perf_counter()
        status = 500
        try:
            response: Response = await call_next(request)
            status = response.status_code
            return response
        except Exception as exc:  # noqa: BLE001
            _log.exception("http.request.error", error=str(exc))
            raise
        finally:
            duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            # Don't log SSE streams as one giant record per second.
            is_stream = request.url.path.endswith("/stream") or request.url.path.endswith("/sse")
            if not is_stream:
                _log.info(
                    "http.request",
                    status=status,
                    duration_ms=duration_ms,
                    query=str(request.url.query) or None,
                    client=str(request.client.host) if request.client else None,
                )
            request_ring.append(
                {
                    "ts": datetime.now().isoformat(timespec="milliseconds"),
                    "request_id": rid,
                    "method": request.method,
                    "path": request.url.path,
                    "query": str(request.url.query) or "",
                    "status": status,
                    "duration_ms": duration_ms,
                    "client": str(request.client.host) if request.client else None,
                    "stream": is_stream,
                }
            )
            # Try to attach the request ID to the response.
            try:
                response.headers["X-Request-ID"] = rid  # type: ignore[name-defined]
            except Exception:  # noqa: BLE001
                pass
            clear_context()
