"""Lightweight process metrics for the debug status endpoint.

We deliberately avoid ``psutil`` (one less dependency); we read what's
freely available from stdlib. ``resource`` is Unix-only — on Windows we
fall back to a smaller set of stats (still pid + uptime + cwd + platform).
"""

from __future__ import annotations

import os
import platform
import sys
import time
from typing import Any

try:
    import resource as _resource
except ImportError:  # Windows
    _resource = None  # type: ignore[assignment]

import app as _app_pkg

_START_TIME = time.monotonic()


def snapshot() -> dict[str, Any]:
    max_rss_kb = None
    user_cpu = None
    sys_cpu = None
    if _resource is not None:
        try:
            rusage = _resource.getrusage(_resource.RUSAGE_SELF)
            max_rss_kb = getattr(rusage, "ru_maxrss", None)
            user_cpu = getattr(rusage, "ru_utime", None)
            sys_cpu = getattr(rusage, "ru_stime", None)
        except Exception:
            pass
    return {
        "version": getattr(_app_pkg, "__version__", "?"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "pid": os.getpid(),
        "uptime_seconds": round(time.monotonic() - _START_TIME, 2),
        "cwd": os.getcwd(),
        "max_rss_kb": max_rss_kb,
        "max_rss_mb_est": round(max_rss_kb / 1024, 1) if max_rss_kb else None,
        "user_cpu_seconds": user_cpu,
        "sys_cpu_seconds": sys_cpu,
    }
