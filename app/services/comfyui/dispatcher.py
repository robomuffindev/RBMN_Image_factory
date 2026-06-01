"""Worker registry + selection across multiple ComfyUI servers."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.logging_setup import get_logger
from app.services.comfyui.client import ComfyUIClient, ComfyUIConnectionError

_log = get_logger("factory.comfy.dispatcher")


@dataclass
class ComfyWorker:
    url: str
    healthy: bool = True
    in_flight: int = 0
    capabilities: set[str] = field(default_factory=lambda: {"klein"})
    last_check: datetime = field(default_factory=datetime.now)
    last_error: str | None = None
    is_runpod: bool = False
    # Consecutive probe failures — used so we don't yank a worker out of
    # rotation because of a single transient blip (e.g. the worker is busy
    # rendering and /system_stats takes longer than the probe timeout).
    # Worker flips to healthy=False only after UNHEALTHY_THRESHOLD failures
    # in a row; a single successful probe resets the counter to 0.
    consecutive_failures: int = 0


class Dispatcher:
    """Process-wide worker registry. Reconfigured when settings change."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workers: dict[str, ComfyWorker] = {}
        self._clients: dict[str, ComfyUIClient] = {}
        self.max_parallel_per_worker = 1

    # ----------------------------------------------------------- configuration

    def configure(self, urls: list[str], max_parallel_per_worker: int = 1) -> None:
        clean = [u.rstrip("/") for u in urls if isinstance(u, str) and u.strip()]
        with self._lock:
            self.max_parallel_per_worker = max(1, int(max_parallel_per_worker))
            for url in list(self._workers.keys()):
                if url not in clean:
                    self._workers.pop(url, None)
                    self._clients.pop(url, None)
            for url in clean:
                if url not in self._workers:
                    self._workers[url] = ComfyWorker(url=url, is_runpod="runpod.net" in url)
        _log.info("comfy.dispatcher.configured", urls=clean, max_per_worker=self.max_parallel_per_worker)

    # ---------------------------------------------------------- health/probe

    # How many consecutive probe failures before we mark a worker unhealthy.
    # 3 = ~90 seconds of pain before yanking it from rotation (probe interval is 30s).
    UNHEALTHY_THRESHOLD = 3

    async def probe_all(self) -> dict[str, dict[str, Any]]:
        """Health-probe every configured worker.

        Behavior intentionally NOT to do:
          - Don't probe workers that are currently in_flight > 0. They're
            obviously alive (running a job) and probing them mid-generation
            risks a slow /system_stats response that LOOKS like a failure.
          - Don't flip healthy=False on a single bad probe. Require
            UNHEALTHY_THRESHOLD consecutive failures (hysteresis) so a
            transient network blip doesn't pull a worker out of rotation.

        Behavior intentionally DO:
          - Reuse the cached ComfyUIClient if we already have one (avoids a
            fresh TCP handshake every 30s).
          - On any success, reset consecutive_failures to 0 immediately.
        """
        results: dict[str, dict[str, Any]] = {}
        loop = asyncio.get_event_loop()
        for url, worker in list(self._workers.items()):
            # Skip live workers — they're proving themselves alive by working.
            if worker.in_flight > 0:
                results[url] = {"healthy": worker.healthy, "skipped": "in_flight"}
                continue

            def _probe(u: str = url) -> dict[str, Any]:
                try:
                    # Prefer the cached client so we don't slam the server with
                    # a fresh connection every 30s.
                    client = self._clients.get(u) or ComfyUIClient(u, skip_health_check=True)
                    stats = client.get_system_stats()
                    self._clients[u] = client
                    return {"healthy": True, "stats": stats}
                except Exception as exc:  # noqa: BLE001
                    return {"healthy": False, "error": str(exc)}
            res = await loop.run_in_executor(None, _probe)
            results[url] = res
            worker.last_check = datetime.now()
            if res.get("healthy"):
                # One success resets the failure counter and restores health.
                if not worker.healthy:
                    _log.info("comfy.dispatcher.worker_recovered", url=url)
                worker.healthy = True
                worker.consecutive_failures = 0
                worker.last_error = None
            else:
                worker.consecutive_failures += 1
                worker.last_error = res.get("error", "")
                if worker.consecutive_failures >= self.UNHEALTHY_THRESHOLD:
                    if worker.healthy:
                        _log.warning(
                            "comfy.dispatcher.worker_unhealthy",
                            url=url,
                            consecutive_failures=worker.consecutive_failures,
                            last_error=worker.last_error,
                        )
                    worker.healthy = False
                else:
                    # Still treat as healthy — just log we missed a beat.
                    _log.info(
                        "comfy.dispatcher.probe_failed_transient",
                        url=url,
                        consecutive_failures=worker.consecutive_failures,
                        threshold=self.UNHEALTHY_THRESHOLD,
                        error=worker.last_error,
                    )
        return results


    # ---------------------------------------------------------------- client

    def get_client(self, url: str) -> ComfyUIClient:
        client = self._clients.get(url)
        if client is None:
            client = ComfyUIClient(url, skip_health_check=True)
            self._clients[url] = client
        return client

    # ------------------------------------------------------- worker selection

    def available_slots(self) -> int:
        total = 0
        for w in self._workers.values():
            if w.healthy:
                total += max(0, self.max_parallel_per_worker - w.in_flight)
        return total

    def capacity_summary(self) -> dict[str, Any]:
        """One-call snapshot of capacity + per-worker breakdown."""
        with self._lock:
            workers = list(self._workers.values())
        configured = len(workers)
        healthy = sum(1 for w in workers if w.healthy)
        in_flight_total = sum(w.in_flight for w in workers)
        free_slots = sum(max(0, self.max_parallel_per_worker - w.in_flight)
                          for w in workers if w.healthy)
        breakdown = [
            {
                "url": w.url,
                "healthy": w.healthy,
                "in_flight": w.in_flight,
                "consecutive_failures": w.consecutive_failures,
                "last_error": w.last_error,
            }
            for w in workers
        ]
        return {
            "configured": configured,
            "healthy": healthy,
            "in_flight_total": in_flight_total,
            "free_slots": free_slots,
            "max_parallel_per_worker": self.max_parallel_per_worker,
            "workers": breakdown,
        }

    def select_worker(self, required_caps: set[str] | None = None, reserve: bool = False) -> ComfyWorker | None:
        with self._lock:
            candidates = [w for w in self._workers.values() if w.healthy]
            if required_caps:
                filtered = [w for w in candidates if required_caps & w.capabilities]
                if filtered:
                    candidates = filtered
            candidates = [w for w in candidates if w.in_flight < self.max_parallel_per_worker]
            if not candidates:
                return None
            # Sort: least busy first; tie-break by most-recent successful probe.
            candidates.sort(key=lambda w: (w.in_flight, -w.last_check.timestamp()))
            chosen = candidates[0]
            if reserve:
                chosen.in_flight += 1
            return chosen

    def release(self, url: str) -> None:
        with self._lock:
            w = self._workers.get(url)
            if w and w.in_flight > 0:
                w.in_flight -= 1

    # ------------------------------------------------------- public iteration

    @property
    def workers(self) -> list[ComfyWorker]:
        """Snapshot list of all configured workers (healthy + unhealthy)."""
        with self._lock:
            return list(self._workers.values())

    @property
    def healthy_workers(self) -> list[ComfyWorker]:
        with self._lock:
            return [w for w in self._workers.values() if w.healthy]

    # ----------------------------------------------------------------- summary

    def summary(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "url": w.url,
                    "healthy": w.healthy,
                    "in_flight": w.in_flight,
                    "is_runpod": w.is_runpod,
                    "last_check": w.last_check.isoformat(timespec="seconds"),
                    "last_error": w.last_error,
                    "consecutive_failures": w.consecutive_failures,
                    "capabilities": sorted(w.capabilities),
                }
                for w in self._workers.values()
            ]


# Process-wide singleton.
dispatcher = Dispatcher()
