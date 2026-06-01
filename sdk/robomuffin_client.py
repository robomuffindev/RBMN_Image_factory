"""Thin Python client for the Robomuffin Image Factory HTTP API.

Designed to be vendored or pip-installed by another local application (e.g.
the WordPress/WooCommerce admin tool described in INTEGRATION.md). All
methods are thin wrappers around documented HTTP endpoints — if a feature
isn't exposed here yet, you can hit the URL directly with `client.session`.

Usage:

    from robomuffin_client import RobomuffinClient

    rb = RobomuffinClient("http://127.0.0.1:8765", api_key="changeme")
    proj = rb.create_project(name="WP product photoshoot Nov 2026")
    img  = rb.create_image(
        project_id=proj["id"],
        prompt="hero shot of the product on a marble counter",
        reference_paths=[r"C:\\share\\wp-uploads\\bottle.png"],
        physical_size="9 inches tall",
        relative_size="a tall water bottle",
    )
    # Block until the image renders, then download it.
    final = rb.wait_for_image(img["id"], timeout=600)
    rb.download_image(img["id"], r"C:\\share\\wp-uploads\\bottle_hero.png")
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

import requests


class RobomuffinError(RuntimeError):
    """Raised when the Robomuffin API returns a non-2xx response."""


class RobomuffinClient:
    """HTTP client for Robomuffin Image Factory.

    Args:
        base_url:    e.g. ``http://127.0.0.1:8765``. Trailing slash optional.
        api_key:     value of the FACTORY_API_KEY env on the Robomuffin host.
                     Pass ``None`` (the default) when the host has no auth.
        timeout:     per-request HTTP timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        if api_key:
            self.session.headers["X-Robomuffin-Key"] = api_key
        # Reasonable default — embedders can override per-call by passing
        # `headers={...}` directly to `session.<verb>(...)`.
        self.session.headers["User-Agent"] = "robomuffin-python-sdk/1"

    # ------------------------------------------------------------------ HTTP
    def _request(self, method: str, path: str, **kw: Any) -> Any:
        url = f"{self.base_url}{path}"
        kw.setdefault("timeout", self.timeout)
        resp = self.session.request(method, url, **kw)
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise RobomuffinError(f"{method} {url} → {resp.status_code}: {detail}")
        # Some endpoints return non-JSON (file downloads). Probe first.
        if resp.headers.get("Content-Type", "").startswith("application/json"):
            return resp.json()
        return resp.content

    # --------------------------------------------------------------- health
    def health(self) -> dict[str, Any]:
        """Hit the /api/debug/status endpoint. Returns a dict with worker
        + queue state. Use this as a liveness probe from the embedding app."""
        return self._request("GET", "/api/debug/status")

    def workers(self) -> dict[str, Any]:
        """Full worker list with per-server health, in_flight count, and
        last_error if any."""
        return self._request("GET", "/api/workers")

    # ------------------------------------------------------------- projects
    def list_projects(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/projects")
        return data.get("items", []) if isinstance(data, dict) else data

    def get_project(self, project_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/projects/{project_id}")

    def create_project(self, name: str, description: str = "") -> dict[str, Any]:
        return self._request("POST", "/api/projects", json={"name": name, "description": description})

    def delete_project(self, project_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/api/projects/{project_id}")

    # --------------------------------------------------------------- images
    def list_images(self, project_id: str) -> list[dict[str, Any]]:
        data = self._request("GET", f"/api/projects/{project_id}/images")
        return data.get("items", [])

    def get_image(self, image_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/images/{image_id}")

    def create_image(
        self,
        project_id: str,
        *,
        prompt: str,
        reference_paths: list[str] | None = None,
        negative_prompt: str = "",
        width: int = 1024,
        height: int = 1024,
        seed: int | None = None,
        physical_size: str | None = None,
        physical_dimensions: str | None = None,
        relative_size: str | None = None,
        output_format: str = "png",
        retain_ref1_name: bool = False,
        custom_output_dir: str | None = None,
        dispatch: bool = True,
    ) -> dict[str, Any]:
        """Create a single image and (by default) queue it for generation.

        References can be either absolute paths on the Robomuffin host OR
        already-uploaded reference filenames. See INTEGRATION.md for the
        upload workflow if your embedding app keeps files in a shared folder.
        """
        body = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "reference_paths": reference_paths or [],
            "width": width,
            "height": height,
            "seed": seed,
            "output_format": output_format,
            "retain_ref1_name": retain_ref1_name,
            "custom_output_dir": custom_output_dir,
            "dispatch": dispatch,
            # Scale hints — passed through the parameters dict for now; the
            # /api/projects/{id}/images endpoint doesn't take them as a
            # first-class field, so use the batch render path for full
            # access to the scale-clause builder.
            "parameters": {
                "physical_size": physical_size,
                "physical_dimensions": physical_dimensions,
                "relative_size": relative_size,
            },
        }
        return self._request("POST", f"/api/projects/{project_id}/images", json=body)

    def download_image(self, image_id: str, out_path: str | Path) -> Path:
        """Stream the rendered output for ``image_id`` to ``out_path``.
        Returns the resolved path. Raises ``RobomuffinError`` if the image
        isn't done yet."""
        resp = self.session.get(
            f"{self.base_url}/api/images/{image_id}/file",
            timeout=self.timeout, stream=True,
        )
        if not resp.ok:
            raise RobomuffinError(f"download {image_id} → {resp.status_code}: {resp.text[:200]}")
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                fh.write(chunk)
        return out

    def rerun_image(
        self,
        image_id: str,
        *,
        prompt: str | None = None,
        negative_prompt: str | None = None,
        reference_paths: list[str] | None = None,
        seed_mode: str | int | None = None,
    ) -> dict[str, Any]:
        """Re-queue an existing image with optional prompt/ref/seed changes.
        The original file is overwritten on completion."""
        return self._request("POST", f"/api/images/{image_id}/rerun", json={
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "reference_paths": reference_paths,
            "seed_mode": seed_mode,
        })

    # ---------------------------------------------------------------- batch
    def commit_batch_render(
        self,
        project_id: str,
        rows: list[dict[str, Any]],
        *,
        output_format: str = "png",
        custom_output_dir: str | None = None,
        reference_base_dir: str | None = None,
        enhance_prompts: bool = False,
        retain_ref1_name: bool = False,
        frame_subject: str | None = None,
    ) -> dict[str, Any]:
        """Submit a batch of rows. Each row is a dict with keys matching the
        BatchRenderRow model (prompt, reference_paths, width, height, seed,
        name, size, dimensions, relative_size). Returns the batch_run_id +
        preflight diagnostics (refs_resolved / refs_missing / refs_unresolved).

        Auto-pauses the dispatcher; call ``release_queue()`` to start render."""
        body = {
            "rows": rows,
            "output_format": output_format,
            "retain_ref1_name": retain_ref1_name,
            "custom_output_dir": custom_output_dir,
            "reference_base_dir": reference_base_dir,
            "enhance_prompts": enhance_prompts,
            "frame_subject": frame_subject,
        }
        return self._request(
            "POST",
            f"/api/batch/render/commit?project_id={project_id}",
            json=body,
        )

    def list_batch_runs(self, project_id: str) -> list[dict[str, Any]]:
        data = self._request("GET", f"/api/batch/runs?project_id={project_id}")
        return data.get("runs", []) if isinstance(data, dict) else data

    # ---------------------------------------------------------------- queue
    def queue_status(self) -> dict[str, Any]:
        return self._request("GET", "/api/jobs/queue/status")

    def release_queue(self) -> dict[str, Any]:
        """Unpause + release every PENDING job onto worker dispatchers."""
        return self._request("POST", "/api/jobs/queue/start")

    def pause_queue(self) -> dict[str, Any]:
        return self._request("POST", "/api/jobs/queue/pause")

    def stop_queue(self) -> dict[str, Any]:
        """Hard stop — cancels everything pending + running + interrupts ComfyUI."""
        return self._request("POST", "/api/jobs/queue/stop")

    # -------------------------------------------------------------- helpers
    def wait_for_image(self, image_id: str, *, timeout: float = 600.0, poll: float = 1.5) -> dict[str, Any]:
        """Poll /api/images/{id} until status == 'done' or 'failed'/'cancelled'.
        Raises ``RobomuffinError`` on failure status or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            img = self.get_image(image_id)
            st = img.get("status")
            if st == "done":
                return img
            if st in ("failed", "cancelled"):
                raise RobomuffinError(f"image {image_id} ended in status={st}: {img.get('error')}")
            time.sleep(poll)
        raise RobomuffinError(f"image {image_id} did not finish within {timeout}s")

    def wait_for_batch(
        self,
        batch_run_id: str,
        project_id: str,
        *,
        timeout: float = 3600.0,
        poll: float = 5.0,
    ) -> dict[str, Any]:
        """Poll until every image in the batch finishes. Returns the final stats."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            runs = self.list_batch_runs(project_id)
            mine = next((r for r in runs if r.get("batch_run_id") == batch_run_id), None)
            if mine:
                c = mine.get("counts", {})
                if c.get("queued", 0) == 0 and c.get("running", 0) == 0:
                    return mine
            time.sleep(poll)
        raise RobomuffinError(f"batch {batch_run_id} did not finish within {timeout}s")
