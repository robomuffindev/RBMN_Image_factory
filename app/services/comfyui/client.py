"""HTTP + WebSocket client for a single ComfyUI server.

Key fix: ONE persistent client_id per client instance, shared between
queue_prompt and stream_prompt. ComfyUI only sends WS messages to the
client_id that submitted the prompt — using different IDs means the
WebSocket sees nothing.
"""

from __future__ import annotations

import json
import ssl
import time
import uuid
from collections.abc import Iterator
from typing import Any

import requests
import urllib3
import websocket

from app.logging_setup import get_logger

_log = get_logger("factory.comfy.client")


class ComfyUIError(Exception): pass
class ComfyUIConnectionError(ComfyUIError): pass
class ComfyUIWorkflowError(ComfyUIError): pass
class ComfyUIVRAMError(ComfyUIError): pass


class ComfyUIClient:
    def __init__(self, base_url: str, timeout: int = 30, skip_health_check: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self._verify = True
        if "runpod.net" in self.base_url or self.base_url.startswith("https://"):
            self._verify = False
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        # ONE persistent client_id per ComfyUIClient instance.
        # MUST be the same for queue_prompt and stream_prompt or WS gets no events.
        self.client_id = str(uuid.uuid4())
        if not skip_health_check:
            try:
                self.get_system_stats()
            except Exception as exc:
                raise ComfyUIConnectionError(f"Health check failed: {exc}") from exc

    def _make_request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{endpoint}"
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("verify", self._verify)
        try:
            resp = self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise ComfyUIConnectionError(f"{method} {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            body = resp.text[:1200]
            _log.warning("comfy.http.error", method=method, url=url, status=resp.status_code, body=body)
            try:
                resp.raise_for_status()
            except Exception as exc:
                raise ComfyUIWorkflowError(f"{method} {url} -> {resp.status_code}: {body}") from exc
        if resp.content and resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return resp

    def get_system_stats(self) -> dict[str, Any]:
        return self._make_request("GET", "/system_stats")

    def queue_prompt(self, workflow: dict[str, Any]) -> dict[str, Any]:
        # Critical: pass self.client_id so the WS subscribed with the same ID
        # actually receives the progress + completion events.
        res = self._make_request("POST", "/prompt", json={"prompt": workflow, "client_id": self.client_id})
        _log.info("comfy.queue_prompt.submitted", prompt_id=res.get("prompt_id"), client_id=self.client_id)
        return res

    def get_history(self, prompt_id: str) -> dict[str, Any]:
        return self._make_request("GET", f"/history/{prompt_id}") or {}

    def upload_image(self, filepath: str, filename: str | None = None) -> dict[str, Any]:
        with open(filepath, "rb") as fh:
            files = {"image": (filename or filepath.split("/")[-1].split("\\")[-1], fh)}
            return self._make_request("POST", "/upload/image", files=files)

    def get_queue(self) -> dict[str, Any]:
        return self._make_request("GET", "/queue")

    def get_object_info(self) -> dict[str, Any]:
        return self._make_request("GET", "/object_info")

    def interrupt(self) -> None:
        try: self._make_request("POST", "/interrupt")
        except Exception: pass

    def free_memory(self) -> None:
        try: self._make_request("POST", "/free", json={"unload_models": True, "free_memory": True})
        except Exception: pass

    def download_output(self, filename: str, subfolder: str = "", type_: str = "output") -> bytes:
        url = f"{self.base_url}/view"
        params = {"filename": filename, "subfolder": subfolder, "type": type_}
        try:
            resp = self.session.get(url, params=params, verify=self._verify, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ComfyUIConnectionError(f"download {filename} failed: {exc}") from exc
        if resp.status_code != 200:
            raise ComfyUIWorkflowError(f"download {filename} -> {resp.status_code}")
        return resp.content

    def try_download_output(self, filename: str, subfolder: str = "", type_: str = "output") -> bytes | None:
        try:
            data = self.download_output(filename, subfolder, type_)
            if data and len(data) > 1000:
                return data
        except Exception:
            pass
        return None

    def stream_prompt(self, prompt_id: str, absolute_timeout: float = 1800.0, recv_timeout: float = 10.0) -> Iterator[dict[str, Any]]:
        """Generator yielding parsed WS messages until prompt completes.

        Uses self.client_id (same one queue_prompt used). All WS messages are
        logged at debug-level so they show up in factory.log + per-job log.
        """
        ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/ws?clientId={self.client_id}"
        ssl_opts: dict[str, Any] = {"cert_reqs": ssl.CERT_NONE} if not self._verify else {}

        _log.info("comfy.ws.connect", url=ws_url, client_id=self.client_id, prompt_id=prompt_id)
        try:
            ws = websocket.create_connection(ws_url, sslopt=ssl_opts or None, timeout=recv_timeout)
        except Exception as exc:
            _log.error("comfy.ws.connect_failed", url=ws_url, error=str(exc))
            raise ComfyUIConnectionError(f"WS connect failed: {exc}") from exc

        saw_progress_complete = False
        idle_cycles = 0
        executed_outputs: dict[str, Any] = {}
        msg_count = 0
        t0 = time.monotonic()
        last_history_check = 0.0
        try:
            while True:
                if time.monotonic() - t0 > absolute_timeout:
                    raise ComfyUIWorkflowError(f"absolute timeout {absolute_timeout}s")
                try:
                    raw = ws.recv()
                except (websocket.WebSocketTimeoutException, TimeoutError):
                    idle_cycles += 1
                    elapsed = round(time.monotonic() - t0, 1)
                    _log.debug("comfy.ws.idle", prompt_id=prompt_id, idle_cycles=idle_cycles, elapsed_s=elapsed, msg_count=msg_count)
                    # Poll history every ~15s regardless — catch completions WS missed.
                    if (time.monotonic() - last_history_check) > 15:
                        last_history_check = time.monotonic()
                        _log.info("comfy.ws.history_probe", prompt_id=prompt_id, elapsed_s=elapsed)
                        hist = self.get_history(prompt_id)
                        if prompt_id in hist:
                            outputs = hist[prompt_id].get("outputs", {})
                            _log.info("comfy.ws.history_complete", prompt_id=prompt_id, output_nodes=list(outputs.keys()))
                            yield {"type": "_complete", "data": {"executed_outputs": outputs}}
                            return
                    continue
                if isinstance(raw, (bytes, bytearray)):
                    continue
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                msg_count += 1
                msg_type = msg.get("type")
                data = msg.get("data", {}) or {}
                if msg_type == "crystools.monitor":
                    continue
                msg_pid = data.get("prompt_id")
                if msg_pid and msg_pid != prompt_id:
                    continue
                # Log every relevant WS frame.
                _log.debug("comfy.ws.msg", prompt_id=prompt_id, type=msg_type, keys=list(data.keys())[:10])
                yield msg

                if msg_type == "progress":
                    if data.get("value", 0) and data.get("max", 0) and data["value"] >= data["max"]:
                        saw_progress_complete = True
                elif msg_type == "executed":
                    node = data.get("node")
                    out = data.get("output") or {}
                    if node is not None:
                        executed_outputs[str(node)] = out
                        _log.info("comfy.ws.executed_node", node=node, output_keys=list(out.keys()))
                elif msg_type == "executing" and data.get("node") is None:
                    _log.info("comfy.ws.executing_done", prompt_id=prompt_id)
                    yield {"type": "_complete", "data": {"executed_outputs": executed_outputs}}
                    return
                elif msg_type in ("execution_complete", "execution_success", "complete"):
                    _log.info("comfy.ws.execution_complete", msg_type=msg_type)
                    yield {"type": "_complete", "data": {"executed_outputs": executed_outputs}}
                    return
                elif msg_type == "execution_error":
                    err = data.get("exception_message") or str(data)
                    _log.error("comfy.ws.execution_error", error=err)
                    if "OutOfMemory" in err or "CUDA out of memory" in err or ("CUDA" in err and "memory" in err.lower()):
                        raise ComfyUIVRAMError(err)
                    raise ComfyUIWorkflowError(err)
                elif msg_type == "status":
                    q = (data.get("status") or {}).get("exec_info", {}).get("queue_remaining")
                    if saw_progress_complete and q == 0:
                        _log.info("comfy.ws.status_queue_drained")
                        yield {"type": "_complete", "data": {"executed_outputs": executed_outputs}}
                        return
        finally:
            try: ws.close()
            except Exception: pass
            _log.info("comfy.ws.closed", prompt_id=prompt_id, msg_count=msg_count, elapsed_s=round(time.monotonic() - t0, 1))
