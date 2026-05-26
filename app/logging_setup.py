"""Application-wide logging configuration.

Important Windows fix: we use ONE writer to ``data/logs/factory.log`` — the
stdlib ``TimedRotatingFileHandler``. structlog routes through stdlib via
``structlog.stdlib.LoggerFactory`` so it doesn't keep its own file handle
open. Without this, Windows can't rotate the log file at midnight because
the rename target is locked by structlog's open() handle.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import traceback
from typing import Any

import structlog
from rich.console import Console
from rich.logging import RichHandler

from app.config import get_settings
from app.services.debug.ring_buffer import error_ring, log_ring


def _ring_buffer_sink(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Mirror every record into in-memory ring buffers + SSE broadcaster."""
    snapshot = dict(event_dict)
    log_ring.append(snapshot)
    level = str(snapshot.get("level", "")).upper()
    if level in {"ERROR", "CRITICAL", "EXCEPTION"} or "exception" in snapshot:
        error_ring.append(snapshot)
    try:
        from app.services.jobs.events import log_event_broadcaster
        log_event_broadcaster.put_nowait(snapshot)
    except Exception:
        pass
    return event_dict


def _ensure_event_string(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    event = event_dict.get("event")
    if event is not None and not isinstance(event, str):
        event_dict["event"] = str(event)
    return event_dict


def _add_exception_summary(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    exc = sys.exc_info()
    if exc and exc[0] is not None and "exception" not in event_dict:
        event_dict["exception"] = "".join(traceback.format_exception(*exc)).strip()
    return event_dict


_CONFIGURED = False


def configure_logging() -> None:
    """Idempotent — safe to call multiple times."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    settings = get_settings()
    settings.ensure_dirs()

    level_name = settings.factory_log_level.upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    # ---- Console handler ----
    if settings.factory_log_rich and sys.stderr.isatty():
        console = Console(stderr=True, force_terminal=True)
        rich_handler = RichHandler(
            console=console,
            show_time=True,
            show_level=True,
            show_path=False,
            rich_tracebacks=True,
            tracebacks_show_locals=False,
            log_time_format="[%X]",
            markup=False,
        )
        rich_handler.setLevel(level)
        root.addHandler(rich_handler)
    else:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setLevel(level)
        stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s"))
        root.addHandler(stream_handler)

    # ---- File handler (single writer, Windows-safe) ----
    # delay=True opens the file on first write, not at construction — that lets
    # the rotator rename without a phantom handle preventing it.
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(settings.app_log_file),
        when="midnight",
        backupCount=max(settings.factory_log_retain_days, 1),
        encoding="utf-8",
        utc=False,
        delay=True,
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(file_handler)

    # ---- Silence chatty libraries so DEBUG isn't a firehose ----
    for noisy in (
        "uvicorn.access",
        "watchfiles",
        "asyncio",
        "httpx",
        "httpcore",
        "websocket",
        "websockets",
        "aiosqlite",
        "sqlalchemy.engine",
        "sqlalchemy.pool",
        "sqlalchemy.dialects",
        "openai",
        "anthropic",
        "google",
    ):
        logging.getLogger(noisy).setLevel(max(level, logging.INFO))

    # ---- structlog: route through stdlib so the file handler is the sole writer ----
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=False),
            _ensure_event_string,
            _add_exception_summary,
            _ring_buffer_sink,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.JSONRenderer() if settings.factory_log_json else structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    _CONFIGURED = True

    log = structlog.get_logger("factory.bootstrap")
    log.info(
        "logging.configured",
        level=level_name,
        json=settings.factory_log_json,
        rich=settings.factory_log_rich,
        log_file=str(settings.app_log_file),
    )


def get_logger(name: str | None = None):
    return structlog.get_logger(name)


def bind_context(**kwargs: Any) -> None:
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()
