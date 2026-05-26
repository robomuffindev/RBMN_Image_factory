"""Per-job log file handler.

For each running Job we attach a logging.Handler that writes a copy of every
log line containing this job_id to ``data/projects/{project_id}/logs/{job_id}.log``.

The runner wraps its per-job execution in ``with job_log_capture(job_id, project_id):``.
Anything logged inside that scope (including from ComfyUI client, dispatcher,
workflow prep — anywhere structlog touches the root logger) lands in the file.

This is the single most useful debugging artifact: a complete forensic trail of
exactly what happened to one specific job, separate from the firehose factory.log.
"""

from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from app.config import get_settings
from app.logging_setup import get_logger

_log = get_logger("factory.job_logger")
_LOCK = threading.Lock()


class _JobFilter(logging.Filter):
    """Only let through records whose structlog context mentions our job_id.

    Structlog merges its contextvars into the log record so the job_id field
    appears as part of the formatted message. We do a simple substring match.
    """

    def __init__(self, job_id: str) -> None:
        super().__init__()
        self.job_id = job_id

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        # contextvars from structlog appear in the formatted line.
        return self.job_id in msg or self.job_id in str(getattr(record, "extra", ""))


def per_job_log_path(project_id: str, job_id: str) -> Path:
    base = get_settings().projects_dir / project_id / "logs"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{job_id}.log"


@contextmanager
def job_log_capture(job_id: str, project_id: str) -> Iterator[Path]:
    """Attach a per-job file handler for the duration of this scope.

    Returns the path to the log file. The handler is removed on exit even if
    an exception is raised, so old jobs don't leak handlers across the process.
    """
    path = per_job_log_path(project_id, job_id)
    header = (
        "=" * 70
        + f"\nJob log: {job_id}\nProject: {project_id}\nStarted: {datetime.now().isoformat(timespec='seconds')}\n"
        + "=" * 70 + "\n"
    )
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(header)
    except Exception as exc:  # noqa: BLE001
        _log.warning("job_logger.header_write_failed", path=str(path), error=str(exc))

    handler = logging.FileHandler(str(path), mode="a", encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s"))
    handler.addFilter(_JobFilter(job_id))
    root = logging.getLogger()
    with _LOCK:
        root.addHandler(handler)
    _log.info("job_logger.attached", job_id=job_id, log_file=str(path))
    try:
        yield path
    finally:
        with _LOCK:
            try:
                root.removeHandler(handler)
                handler.close()
            except Exception:
                pass
        _log.info("job_logger.detached", job_id=job_id, log_file=str(path))


def read_job_log(project_id: str, job_id: str, tail: int = 500) -> dict[str, object]:
    """Return the last `tail` lines of a job's log, or {} if missing."""
    path = per_job_log_path(project_id, job_id)
    if not path.exists():
        return {"exists": False, "path": str(path), "lines": []}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        return {"exists": True, "path": str(path), "error": str(exc), "lines": []}
    return {"exists": True, "path": str(path), "size_bytes": path.stat().st_size, "lines": lines[-tail:]}
