"""Per-call LLM forensic logger.

Every LLM call routed through ``PromptEnhancer`` should be wrapped with
``LLMCallLog.start()`` / ``log.finish()`` (or the ``log_llm_call`` context
manager). Each call produces:

1. A line in the main structlog stream (``llm.call.start`` / ``.finish`` /
   ``.error``) carrying ``llm_call_id``, provider, model, latency.
2. A JSON file at ``data/logs/llm/YYYY-MM-DD/{timestamp}_{call_id}.json``
   with the *full* system prompt, user message, raw response, token usage,
   and any error.

This makes "why did this prompt come out weird?" answerable forever after.
The files are kept indefinitely until the user prunes them.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from app.config import get_settings
from app.logging_setup import get_logger


_log = get_logger("factory.llm")


@dataclass
class LLMCallRecord:
    call_id: str
    provider: str
    model: str
    purpose: str                       # "enhance_image" | "enhance_bulk" | "test" | ...
    system_prompt: str
    user_message: str
    request_meta: dict[str, Any] = field(default_factory=dict)
    response_text: str = ""
    response_meta: dict[str, Any] = field(default_factory=dict)
    tokens_input: int | None = None
    tokens_output: int | None = None
    started_at: str = ""
    finished_at: str = ""
    latency_ms: float | None = None
    error: str | None = None
    project_id: str | None = None
    image_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _path_for(record: LLMCallRecord) -> Path:
    settings = get_settings()
    day = record.started_at[:10] if record.started_at else datetime.now().strftime("%Y-%m-%d")
    dir_ = settings.llm_logs_dir / day
    dir_.mkdir(parents=True, exist_ok=True)
    stem = f"{record.started_at.replace(':', '-').replace('.', '-')}_{record.call_id[:8]}"
    return dir_ / f"{stem}.json"


def _write(record: LLMCallRecord) -> Path:
    path = _path_for(record)
    path.write_text(json.dumps(record.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def list_recent(limit: int = 50) -> list[dict[str, Any]]:
    """Return the last `limit` LLM call summaries (without full prompt text)."""
    settings = get_settings()
    if not settings.llm_logs_dir.exists():
        return []
    files = sorted(settings.llm_logs_dir.rglob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, Any]] = []
    for f in files[:limit]:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            _log.warning("llm.log.read_failed", path=str(f), error=str(exc))
            continue
        out.append(
            {
                "call_id": data.get("call_id"),
                "provider": data.get("provider"),
                "model": data.get("model"),
                "purpose": data.get("purpose"),
                "started_at": data.get("started_at"),
                "latency_ms": data.get("latency_ms"),
                "tokens_input": data.get("tokens_input"),
                "tokens_output": data.get("tokens_output"),
                "error": data.get("error"),
                "path": str(f),
            }
        )
    return out


def read_call(call_id: str) -> dict[str, Any] | None:
    """Return the full record (incl. prompt + response) for a single call_id."""
    settings = get_settings()
    if not settings.llm_logs_dir.exists():
        return None
    for f in settings.llm_logs_dir.rglob(f"*{call_id[:8]}*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("call_id") == call_id:
                return data
        except Exception:  # noqa: BLE001
            continue
    return None


@contextmanager
def log_llm_call(
    *,
    provider: str,
    model: str,
    purpose: str,
    system_prompt: str,
    user_message: str,
    request_meta: dict[str, Any] | None = None,
    project_id: str | None = None,
    image_id: str | None = None,
) -> Iterator[LLMCallRecord]:
    """Context manager around a single LLM call.

    Usage::

        with log_llm_call(provider="openai", model="gpt-4o",
                          purpose="enhance_image",
                          system_prompt=sys_p, user_message=user_m) as rec:
            response = client.chat.completions.create(...)
            rec.response_text = response.choices[0].message.content
            rec.tokens_input = response.usage.prompt_tokens
            rec.tokens_output = response.usage.completion_tokens
    """
    call_id = str(uuid.uuid4())
    started_dt = datetime.now()
    record = LLMCallRecord(
        call_id=call_id,
        provider=provider,
        model=model,
        purpose=purpose,
        system_prompt=system_prompt,
        user_message=user_message,
        request_meta=request_meta or {},
        started_at=started_dt.isoformat(timespec="milliseconds"),
        project_id=project_id,
        image_id=image_id,
    )
    t0 = time.perf_counter()
    _log.info(
        "llm.call.start",
        llm_call_id=call_id,
        provider=provider,
        model=model,
        purpose=purpose,
        project_id=project_id,
        image_id=image_id,
    )
    try:
        yield record
    except Exception as exc:  # noqa: BLE001
        record.error = f"{type(exc).__name__}: {exc}"
        record.finished_at = datetime.now().isoformat(timespec="milliseconds")
        record.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        path = _write(record)
        _log.error(
            "llm.call.error",
            llm_call_id=call_id,
            provider=provider,
            model=model,
            error=record.error,
            latency_ms=record.latency_ms,
            log_file=str(path),
        )
        raise
    else:
        record.finished_at = datetime.now().isoformat(timespec="milliseconds")
        record.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        path = _write(record)
        _log.info(
            "llm.call.finish",
            llm_call_id=call_id,
            provider=provider,
            model=model,
            latency_ms=record.latency_ms,
            tokens_input=record.tokens_input,
            tokens_output=record.tokens_output,
            log_file=str(path),
        )
