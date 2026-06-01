"""Job dispatch loop.

Runs as a long-lived background asyncio task started from FastAPI's
lifespan. For each pending Job:

1. Reserve a worker (filtered by required capabilities).
2. Upload reference images to that ComfyUI server.
3. Build the workflow JSON (with anti-text suffix, fixups).
4. Submit and stream progress via WebSocket → broadcast SSE events.
5. Download outputs and write Asset record on completion.
6. Retry on VRAM/connection errors (max 3, exponential backoff).
"""

from __future__ import annotations

import asyncio
import random
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import PROJECT_ROOT, get_settings
from app.db.engine import get_sessionmaker
from app.db.models import Image, ImageStatus, Job, JobStatus, Project
from app.logging_setup import bind_context, clear_context, get_logger
from app.services.debug.job_logger import job_log_capture
from app.services.comfyui.client import (
    ComfyUIConnectionError,
    ComfyUIVRAMError,
    ComfyUIWorkflowError,
)
from app.services.comfyui.dispatcher import dispatcher
from app.services.comfyui.workflow import (
    flatten_group_nodes,
    klein_workflow_path_for_refs,
    load_workflow,
    prepare_klein_workflow,
    prepare_qwen_workflow,
    qwen_workflow_path_for_refs,
    remove_missing_nodes,
    stamp_unique_filename_prefix,
    workflow_path_for_model,
)
from app.services.jobs.events import job_event_broadcaster
from app.services.jobs.queue import queue, queue_summary, recover_running_jobs, repopulate_from_db

_log = get_logger("factory.runner")

MAX_RETRIES = 3
WORKFLOWS_DIR = PROJECT_ROOT / "app" / "services" / "comfyui" / "workflows"


def _broadcast(event: str, payload: dict[str, Any]) -> None:
    job_event_broadcaster.put_nowait({"_event_name": event, **payload})


async def _set_job_status(job_id: str, status: JobStatus, **fields: Any) -> Job | None:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        if job is None:
            return None
        job.status = status
        for k, v in fields.items():
            setattr(job, k, v)
        if status == JobStatus.RUNNING and job.started_at is None:
            job.started_at = datetime.now().isoformat(timespec="seconds")
        if status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
            job.completed_at = datetime.now().isoformat(timespec="seconds")
        await session.commit()
        await session.refresh(job)
        return job


async def _set_image_status(image_id: str, status: ImageStatus, **fields: Any) -> Image | None:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as session:
        img = await session.get(Image, image_id)
        if img is None:
            return None
        img.status = status
        for k, v in fields.items():
            setattr(img, k, v)
        if status == ImageStatus.DONE:
            img.completed_at = datetime.now().isoformat(timespec="seconds")
        await session.commit()
        await session.refresh(img)
        return img


def _project_outputs_dir(project_id: str) -> Path:
    return get_settings().projects_dir / project_id / "outputs"


def _upload_refs(client, image_paths: list[str]) -> list[str]:
    """Upload local reference image paths to the ComfyUI worker.

    ComfyUI's LoadImage validator rejects WebP / GIF / BMP / TIFF on many
    builds — even though PIL can read them. To dodge that, we normalize
    EVERY reference to PNG via Pillow before uploading. PNG is the
    one format ComfyUI's LoadImage accepts everywhere.

    Behavior:
      1. If the file exists locally → open with PIL → re-encode as PNG bytes →
         write to a temp file → upload that → use the returned name in the workflow.
      2. If PIL can't open it → fall back to uploading the original bytes.
      3. If the file doesn't exist locally → log loudly + pass bare filename
         (only works if the file is already in ComfyUI's input/ folder).
    """
    import tempfile
    from io import BytesIO

    out: list[str] = []
    for p in image_paths[:4]:
        if not p:
            continue
        local = Path(p)
        if not (local.exists() and local.is_file()):
            _log.warning(
                "runner.ref_missing_locally", path=p,
                hint="path doesn't exist on the Robomuffin host — falling back to bare filename, "
                     "which only works if the file is already in the ComfyUI input/ folder",
            )
            out.append(local.name)
            continue

        size = local.stat().st_size
        ext = local.suffix.lower()
        _log.info("runner.ref_upload.start", path=str(local), size_bytes=size, ext=ext)

        # Extensions that ComfyUI's LoadImage validator hard-rejects on many builds.
        # If conversion to PNG fails for these we want to fail loudly, NOT upload the original.
        unsupported_native = {".webp", ".gif", ".bmp", ".tiff", ".tif", ".avif", ".heic", ".heif"}

        # ---- Normalize to PNG via Pillow ----
        import uuid as _uuid
        tmp_path: Path | None = None
        upload_path = str(local)
        upload_name = local.name
        normalized = False
        try:
            from PIL import Image as PILImage
            with PILImage.open(local) as im:
                im.load()
                if im.mode not in ("RGB", "RGBA"):
                    im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
                buf = BytesIO()
                im.save(buf, "PNG", optimize=True)
                buf.seek(0)
            tmp = tempfile.NamedTemporaryFile(prefix="rbmn_ref_", suffix=".png", delete=False)
            tmp.write(buf.getvalue())
            tmp.close()
            tmp_path = Path(tmp.name)
            upload_path = str(tmp_path)
            # Unique upload name so we never collide with a previously-cached
            # version on the ComfyUI server (e.g. a stale ".webp" from a failed run).
            upload_name = f"{local.stem}_{_uuid.uuid4().hex[:8]}.png"
            normalized = True
            _log.info("runner.ref_normalized_to_png",
                      original=str(local), tmp=upload_path,
                      upload_name=upload_name,
                      orig_bytes=size, new_bytes=tmp_path.stat().st_size)
        except Exception as exc:  # noqa: BLE001
            _log.warning("runner.ref_normalize_failed", path=str(local), error=str(exc))
            if ext in unsupported_native:
                # Don't silently upload a format ComfyUI will reject — fail the job clearly.
                msg = (
                    f"Reference image {local.name!r} could not be converted to PNG "
                    f"({exc!s}). ComfyUI's LoadImage rejects {ext} natively. "
                    f"Re-save the image as PNG/JPG manually and retry."
                )
                _log.error("runner.ref_unsupported_format", path=str(local), ext=ext)
                raise ComfyUIWorkflowError(msg)

        try:
            res = client.upload_image(upload_path, upload_name)
            remote = res.get("name") or upload_name
            _log.info("runner.ref_upload.ok", path=str(local), remote=remote, normalized=normalized)
            out.append(remote)
        except Exception as exc:  # noqa: BLE001
            _log.warning("runner.ref_upload.failed", path=str(local), error=str(exc))
            out.append(local.name)
        finally:
            if tmp_path is not None:
                try: tmp_path.unlink()
                except Exception: pass
    return out


async def _run_via_gpt_image(job_id: str, job, image, project, s_row) -> None:
    """Generate an image via OpenAI's GPT Image API and write it to disk.

    Mirrors the same output-handling tail as the ComfyUI path: respects
    custom_output_dir, retain_ref1_name, output_format (PNG/JPG/WebP), and
    writes the batch_run.log entry. Broadcasts the same SSE events so the UI
    updates the same way regardless of which engine produced the image.
    """
    from app.services.openai_images.client import generate_with_gpt_image

    await _set_job_status(job_id, JobStatus.RUNNING, worker_url="openai:gpt-image")
    await _set_image_status(image.id, ImageStatus.RUNNING)
    _broadcast("job_started", {
        "job_id": job_id, "image_id": image.id, "project_id": project.id,
        "worker_url": "openai:gpt-image", "status": "running",
    })
    _broadcast("job_progress", {"job_id": job_id, "image_id": image.id, "status": "running", "progress": 5})

    api_key = (s_row.openai_api_key or "").strip()
    if not api_key:
        raise RuntimeError("GPT Image requires an OpenAI API key in Settings.")

    png_bytes = await generate_with_gpt_image(
        api_key=api_key,
        prompt=getattr(image, "_framed_prompt", None) or image.enhanced_prompt or image.prompt,
        reference_paths=image.reference_paths or [],
        model=s_row.gpt_image_model or "gpt-image-2",
        size=s_row.gpt_image_size or "auto",
        quality=s_row.gpt_image_quality or "auto",
        project_id=project.id,
        image_id=image.id,
    )

    # ---- Output destination (same logic as ComfyUI path) ----
    out_dir = Path(image.custom_output_dir) if image.custom_output_dir else _project_outputs_dir(project.id)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        _log.warning("runner.outdir.mkdir_failed", dir=str(out_dir), error=str(exc), fallback="project_outputs")
        out_dir = _project_outputs_dir(project.id)
        out_dir.mkdir(parents=True, exist_ok=True)

    out_format = (image.output_format or "png").lower()
    if out_format not in ("png", "jpg", "jpeg", "webp"):
        out_format = "png"
    ext = {"png": ".png", "jpg": ".jpg", "jpeg": ".jpg", "webp": ".webp"}[out_format]

    if image.retain_ref1_name and image.reference_paths:
        stem = Path(image.reference_paths[0]).stem or image.id[:8]
        base_stem = stem; attempt = 1
        while (out_dir / f"{stem}{ext}").exists():
            attempt += 1; stem = f"{base_stem}_{attempt}"
    else:
        stem = f"{image.id[:8]}_gpt"
    local_path = out_dir / f"{stem}{ext}"

    if out_format == "png":
        local_path.write_bytes(png_bytes)
    else:
        try:
            from io import BytesIO
            from PIL import Image as PILImage
            im = PILImage.open(BytesIO(png_bytes))
            if out_format in ("jpg", "jpeg"):
                if im.mode in ("RGBA", "P"): im = im.convert("RGB")
                im.save(local_path, "JPEG", quality=92, optimize=True, progressive=True)
            else:
                im.save(local_path, "WEBP", quality=90, method=6)
        except Exception as exc:
            _log.warning("runner.gpt_output.convert_failed", error=str(exc), fallback="png")
            local_path = local_path.with_suffix(".png"); local_path.write_bytes(png_bytes)

    # Thumbnail (best-effort) — stored in a `.thumbnails/` subfolder so the
    # user's output dir contains exactly one file per generated image, not
    # two. A 176-image batch was producing ~352 files because every output
    # was paired with a `thumb_*.jpg` sibling. Hidden dir keeps Windows
    # Explorer / Finder out of the way for users browsing renders.
    thumb_path = None
    try:
        from PIL import Image as PILImage
        thumbs_dir = out_dir / ".thumbnails"
        thumbs_dir.mkdir(parents=True, exist_ok=True)
        with PILImage.open(local_path) as im:
            im.thumbnail((512, 512))
            tp = thumbs_dir / f"thumb_{local_path.stem}.jpg"
            if im.mode in ("RGBA", "P"): im = im.convert("RGB")
            im.save(tp, "JPEG", quality=85)
            thumb_path = str(tp)
    except Exception as exc:
        _log.warning("runner.thumbnail.failed", error=str(exc))

    if image.batch_run_id:
        try:
            with (out_dir / "batch_run.log").open("a", encoding="utf-8") as fh:
                fh.write(
                    f"{datetime.now().isoformat(timespec='seconds')} engine=gpt_image "
                    f"image={image.id[:8]} status=done prompt={(image.prompt or '')[:80]!r} "
                    f"out={local_path.name}\n"
                )
        except Exception as exc:
            _log.warning("runner.batch_log.write_failed", error=str(exc))

    await _set_image_status(image.id, ImageStatus.DONE,
                            output_path=str(local_path), thumbnail_path=thumb_path, error=None)
    await _set_job_status(job_id, JobStatus.DONE,
                          result={"output_path": str(local_path), "engine": "gpt_image"})
    _broadcast("job_completed", {
        "job_id": job_id, "image_id": image.id, "project_id": project.id,
        "status": "done", "output_path": f"/api/images/{image.id}/file",
    })
    _log.info("runner.gpt_image.done", path=str(local_path))


async def _process_one(job_id: str) -> None:
    """Run one job to completion (or failure / cancellation)."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        if job is None:
            return
        if job.status == JobStatus.CANCELLED:
            return
        image = await session.get(Image, job.image_id)
        project = await session.get(Project, job.project_id)

    if job is None or image is None or project is None:
        await _set_job_status(job_id, JobStatus.FAILED, error="missing image/project")
        return

    bind_context(job_id=job_id, image_id=image.id, project_id=project.id)
    _log.info("runner.job.start", retry=job.retry_count)
    # Open the per-job log file. Anything logged inside this scope that mentions
    # the job_id (via bind_context or explicit kwarg) lands at
    # data/projects/{project_id}/logs/{job_id}.log
    _job_log_ctx = job_log_capture(job_id, project.id)
    _job_log_ctx.__enter__()

    # ─── Engine routing ──────────────────────────────────────────────────────
    # Read AppSettings to decide whether to use ComfyUI or GPT Image API.
    from app.services import settings_service
    from app.services.framing import apply_framing, is_enabled as _framing_enabled, build_scale_clause
    async with get_sessionmaker()() as session:
        s_row = await settings_service.get_or_create(session)
    use_gpt = bool(getattr(s_row, "use_gpt_image", False))

    # ─── Prompt framing scaffolds ────────────────────────────────────────────
    # Augment the prompt + negative with composition language so the subject
    # doesn't end up tiny in the frame. Resolves per-image override against
    # the global default — see app/services/framing.py for the scaffolds.
    framing_on = _framing_enabled(
        per_image=getattr(image, "frame_subject", None),
        global_default=bool(getattr(s_row, "frame_subject_default", False)),
    )
    # Scale hint — appended BEFORE framing so the framing scaffold (if any)
    # comes last in the final prompt. Built from the three optional CSV/JSON
    # import columns: physical_size, physical_dimensions, relative_size.
    # See app/services/framing.py:build_scale_clause for the wording.
    _base_prompt = image.enhanced_prompt or image.prompt or ""
    _scale = build_scale_clause(
        physical_size=getattr(image, "physical_size", None),
        physical_dimensions=getattr(image, "physical_dimensions", None),
        relative_size=getattr(image, "relative_size", None),
    )
    if _scale:
        _base_prompt = _base_prompt.rstrip(" ,.") + _scale
        _log.info(
            "runner.scale.applied",
            image_id=image.id,
            physical_size=getattr(image, "physical_size", None),
            physical_dimensions=getattr(image, "physical_dimensions", None),
            relative_size=getattr(image, "relative_size", None),
        )
    framed = apply_framing(
        prompt=_base_prompt,
        negative=image.negative_prompt or "",
        enabled=framing_on,
        strength=getattr(s_row, "frame_subject_strength", "moderate") or "moderate",
        positive_override=getattr(s_row, "frame_subject_positive_override", None),
        negative_override=getattr(s_row, "frame_subject_negative_override", None),
    )
    if framed.applied:
        _log.info(
            "runner.framing.applied",
            image_id=image.id, strength=framed.strength,
            final_prompt_len=len(framed.prompt),
            final_negative_len=len(framed.negative),
        )
        # Stash the framed text on the image object for the rest of this run.
        # We deliberately do NOT persist this to DB — original prompt stays
        # editable, framing is re-applied on every dispatch.
        image._framed_prompt = framed.prompt
        image._framed_negative = framed.negative
    else:
        image._framed_prompt = (image.enhanced_prompt or image.prompt)
        image._framed_negative = image.negative_prompt or ""
    if use_gpt:
        _log.info("runner.engine.gpt_image", model=s_row.gpt_image_model)
        try:
            await _run_via_gpt_image(job_id, job, image, project, s_row)
        except Exception as exc:  # noqa: BLE001
            _log.exception("runner.gpt_image.failed", error=str(exc))
            await _retry_or_fail(job_id, image.id, str(exc))
        finally:
            try: _job_log_ctx.__exit__(None, None, None)
            except Exception: pass
            clear_context()
        return

    worker = dispatcher.select_worker(required_caps={"klein"}, reserve=True)
    if worker is None:
        await asyncio.sleep(1.0)
        # Push back onto the queue. NO release needed — reserve only happens
        # when a non-None worker is returned, so in_flight wasn't incremented.
        await queue.enqueue(job_id, job.priority)
        clear_context()
        return

    # CRITICAL: the worker is now reserved (in_flight += 1). Every code path
    # from here on MUST hit the finally that calls dispatcher.release().
    # The previous version had `await _set_job_status / _set_image_status /
    # _broadcast` OUTSIDE the try-block — if the task was cancelled or any of
    # those awaits raised (e.g. SQLite busy), the finally never ran and the
    # worker's in_flight counter stayed permanently incremented. Over time,
    # one such leak per worker dropped servers out of rotation. The try now
    # wraps EVERYTHING after the reserve.
    try:
        await _set_job_status(job_id, JobStatus.RUNNING, worker_url=worker.url)
        await _set_image_status(image.id, ImageStatus.RUNNING)
        _broadcast("job_started", {"job_id": job_id, "image_id": image.id, "project_id": project.id, "worker_url": worker.url, "status": "running"})
        client = dispatcher.get_client(worker.url)
        loop = asyncio.get_event_loop()

        ref_paths = image.reference_paths or []
        # Pick the workflow JSON based on the configured image_model_type
        # (flux2_klein_9b → klein_*ref.json, qwen_edit_2511 → qwen_*ref.json).
        _model_type = (getattr(s_row, "image_model_type", "flux2_klein_9b") or "flux2_klein_9b")
        _qwen_variant = (getattr(s_row, "qwen_gguf_variant", "Q5_K_S") or "Q5_K_S")
        wf_path = workflow_path_for_model(WORKFLOWS_DIR, _model_type, len(ref_paths))
        if not wf_path.exists():
            raise ComfyUIWorkflowError(f"workflow not found: {wf_path}")
        _log.info("runner.workflow.selected", model_type=_model_type, wf=str(wf_path.name),
                  ref_count=len(ref_paths), qwen_variant=_qwen_variant if _model_type == "qwen_edit_2511" else None)

        remote_refs = await loop.run_in_executor(None, lambda: _upload_refs(client, ref_paths))
        seed = image.seed if image.seed is not None else random.randint(1, 2_000_000_000)

        def _build_and_submit() -> tuple[str, dict[str, Any]]:
            # Branch to the correct workflow preparer.
            if _model_type == "qwen_edit_2511":
                wf = prepare_qwen_workflow(
                    wf_path,
                    prompt=getattr(image, "_framed_prompt", None) or image.enhanced_prompt or image.prompt,
                    width=image.width,
                    height=image.height,
                    seed=seed,
                    ref_images=remote_refs,
                    negative_prompt=getattr(image, "_framed_negative", None) or image.negative_prompt,
                    gguf_variant=_qwen_variant,
                )
            else:
                wf = prepare_klein_workflow(
                    wf_path,
                    prompt=getattr(image, "_framed_prompt", None) or image.enhanced_prompt or image.prompt,
                    width=image.width,
                    height=image.height,
                    seed=seed,
                    ref_images=remote_refs,
                    negative_prompt=getattr(image, "_framed_negative", None) or image.negative_prompt,
                )
            wf = flatten_group_nodes(wf)
            # Capability discovery — best-effort.
            try:
                available = set(client.get_object_info().keys())
                remove_missing_nodes(wf, available)
            except Exception as exc:  # noqa: BLE001
                _log.warning("runner.object_info.failed", error=str(exc))
            unique_tag = f"rbmn_{job_id[:8]}_{int(time.time())}"
            stamp_unique_filename_prefix(wf, unique_tag)
            submit = client.queue_prompt(wf)
            return submit.get("prompt_id"), {"unique_tag": unique_tag, "workflow": wf}

        prompt_id, extra = await loop.run_in_executor(None, _build_and_submit)
        if not prompt_id:
            raise ComfyUIWorkflowError("ComfyUI returned no prompt_id")
        await _set_job_status(job_id, JobStatus.RUNNING, prompt_id=prompt_id)
        _broadcast("job_progress", {"job_id": job_id, "image_id": image.id, "status": "running", "progress": 0})

        # Stream WS events in a thread → push back via asyncio Queue.
        events_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        stop_marker = object()

        def _stream() -> None:
            try:
                for msg in client.stream_prompt(prompt_id):
                    asyncio.run_coroutine_threadsafe(events_q.put(msg), loop)
            except Exception as exc:  # noqa: BLE001
                asyncio.run_coroutine_threadsafe(events_q.put({"_error": exc}), loop)
            finally:
                asyncio.run_coroutine_threadsafe(events_q.put(stop_marker), loop)

        await loop.run_in_executor(None, lambda: None)
        loop.run_in_executor(None, _stream)

        executed_outputs: dict[str, Any] = {}
        while True:
            msg = await events_q.get()
            if msg is stop_marker:
                break
            if isinstance(msg, dict) and "_error" in msg:
                raise msg["_error"]
            mtype = msg.get("type")
            data = msg.get("data") or {}
            if mtype == "progress":
                val, mx = data.get("value", 0), data.get("max", 0)
                pct = int((val / mx) * 100) if mx else 0
                _broadcast("job_progress", {"job_id": job_id, "image_id": image.id, "status": "running", "progress": pct, "node": data.get("node")})
            elif mtype == "_complete":
                executed_outputs = data.get("executed_outputs", {})
                break

        # Find an output filename — prefer /history, fall back to WS-captured outputs.
        output_filename: str | None = None
        output_subfolder = ""

        def _scan_history() -> tuple[str | None, str]:
            for _ in range(10):
                hist = client.get_history(prompt_id)
                node = hist.get(prompt_id, {}).get("outputs", {})
                for _node_id, out in node.items():
                    for img_info in (out.get("images") or []):
                        return img_info.get("filename"), img_info.get("subfolder", "")
                time.sleep(2)
            return None, ""

        output_filename, output_subfolder = await loop.run_in_executor(None, _scan_history)
        if not output_filename:
            for _node_id, out in executed_outputs.items():
                for img_info in (out.get("images") or []):
                    output_filename = img_info.get("filename")
                    output_subfolder = img_info.get("subfolder", "")
                    break
                if output_filename:
                    break

        if not output_filename:
            # Last-ditch: try the unique prefix.
            tag = extra["unique_tag"]
            guessed = f"{tag}_00001_.png"
            data = await loop.run_in_executor(None, lambda: client.try_download_output(guessed))
            if data:
                output_filename = guessed
                output_bytes = data
            else:
                raise ComfyUIWorkflowError("no output filename found in history or WS")
        else:
            output_bytes = await loop.run_in_executor(
                None, lambda: client.download_output(output_filename, output_subfolder)
            )

        # ---- Output destination: honor custom_output_dir + retain_ref1_name + output_format
        out_dir = Path(image.custom_output_dir) if image.custom_output_dir else _project_outputs_dir(project.id)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            _log.warning("runner.outdir.mkdir_failed", dir=str(out_dir), error=str(exc), fallback="project_outputs")
            out_dir = _project_outputs_dir(project.id)
            out_dir.mkdir(parents=True, exist_ok=True)

        out_format = (image.output_format or "png").lower()
        if out_format not in ("png", "jpg", "jpeg", "webp"):
            out_format = "png"
        ext = {"png": ".png", "jpg": ".jpg", "jpeg": ".jpg", "webp": ".webp"}[out_format]

        # Filename: ref1 stem if retain_ref1_name else id_seed
        if image.retain_ref1_name and image.reference_paths:
            stem = Path(image.reference_paths[0]).stem or f"{image.id[:8]}"
            # Collision guard — if a file with this stem already exists in out_dir,
            # append _2, _3, … so we never silently overwrite user files.
            base_stem = stem
            attempt = 1
            while (out_dir / f"{stem}{ext}").exists():
                attempt += 1
                stem = f"{base_stem}_{attempt}"
        else:
            stem = f"{image.id[:8]}_{seed}"
        local_path = out_dir / f"{stem}{ext}"

        # Write the bytes — convert format via PIL if needed.
        if out_format == "png":
            local_path.write_bytes(output_bytes)
        else:
            try:
                from io import BytesIO
                from PIL import Image as PILImage
                im = PILImage.open(BytesIO(output_bytes))
                save_kwargs: dict[str, Any] = {}
                if out_format in ("jpg", "jpeg"):
                    if im.mode in ("RGBA", "P"):
                        im = im.convert("RGB")
                    save_kwargs = {"quality": 92, "optimize": True, "progressive": True}
                    im.save(local_path, "JPEG", **save_kwargs)
                elif out_format == "webp":
                    save_kwargs = {"quality": 90, "method": 6}
                    im.save(local_path, "WEBP", **save_kwargs)
                _log.info("runner.output.converted", from_="png", to=out_format, path=str(local_path))
            except Exception as exc:
                _log.warning("runner.output.convert_failed", error=str(exc), fallback="png")
                local_path = local_path.with_suffix(".png")
                local_path.write_bytes(output_bytes)

        # Thumbnail (best-effort) — written into `.thumbnails/` subfolder so
        # the user's output dir contains exactly one file per generated
        # image. Pre-fix behaviour wrote `thumb_*.jpg` siblings into the
        # same folder, doubling the visible file count.
        thumb_path: str | None = None
        try:
            from PIL import Image as PILImage
            thumbs_dir = out_dir / ".thumbnails"
            thumbs_dir.mkdir(parents=True, exist_ok=True)
            with PILImage.open(local_path) as im:
                im.thumbnail((512, 512))
                tp = thumbs_dir / f"thumb_{local_path.stem}.jpg"
                if im.mode in ("RGBA", "P"):
                    im = im.convert("RGB")
                im.save(tp, "JPEG", quality=85)
                thumb_path = str(tp)
        except Exception as exc:  # noqa: BLE001
            _log.warning("runner.thumbnail.failed", error=str(exc))

        # Batch run log (one line per image into batch_run.log in the output dir)
        if image.batch_run_id:
            try:
                log_path = out_dir / "batch_run.log"
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(
                        f"{datetime.now().isoformat(timespec='seconds')} "
                        f"image={image.id[:8]} status=done prompt={(image.prompt or '')[:80]!r} "
                        f"out={local_path.name}\n"
                    )
            except Exception as exc:
                _log.warning("runner.batch_log.write_failed", error=str(exc))

        await _set_image_status(
            image.id,
            ImageStatus.DONE,
            output_path=str(local_path),
            thumbnail_path=thumb_path,
            seed=seed,
            error=None,
        )
        await _set_job_status(job_id, JobStatus.DONE, result={"output_path": str(local_path), "filename": output_filename})
        _broadcast("job_completed", {"job_id": job_id, "image_id": image.id, "project_id": project.id, "status": "done", "output_path": f"/api/images/{image.id}/file"})
        _log.info("runner.job.done", path=str(local_path))

    except ComfyUIVRAMError as exc:
        _log.warning("runner.vram_error", error=str(exc), retry=job.retry_count)
        try:
            client.free_memory()
        except Exception:
            pass
        await _retry_or_fail(job_id, image.id, str(exc))
    except ComfyUIConnectionError as exc:
        _log.warning("runner.connection_error", error=str(exc), retry=job.retry_count)
        await _retry_or_fail(job_id, image.id, str(exc))
    except (ComfyUIWorkflowError, Exception) as exc:
        _log.exception("runner.job.failed", error=str(exc))
        await _retry_or_fail(job_id, image.id, str(exc))
    finally:
        dispatcher.release(worker.url)
        try:
            _job_log_ctx.__exit__(None, None, None)
        except Exception:
            pass
        clear_context()


async def _retry_or_fail(job_id: str, image_id: str, error: str) -> None:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        if job is None:
            return
        job.retry_count += 1
        if job.retry_count >= MAX_RETRIES:
            job.status = JobStatus.FAILED
            job.error = error
            job.completed_at = datetime.now().isoformat(timespec="seconds")
            img = await session.get(Image, image_id)
            if img:
                img.status = ImageStatus.FAILED
                img.error = error
            await session.commit()
            _broadcast("job_failed", {"job_id": job_id, "image_id": image_id, "status": "failed", "error": error})
        else:
            job.status = JobStatus.PENDING
            job.error = error
            await session.commit()
            _broadcast("job_retrying", {"job_id": job_id, "image_id": image_id, "status": "retrying", "retry_count": job.retry_count})


# ---------------------------------------------------------------------------
# Runner background loop
# ---------------------------------------------------------------------------

_runner_task: asyncio.Task | None = None
_stop_requested = False


async def _process_one_safely(job_id: str) -> None:
    """Wrapper around `_process_one` that funnels any unhandled exception
    into the retry/fail pipeline. Used by the parallel dispatcher so a
    single misbehaving job can't kill the runner task."""
    try:
        await _process_one(job_id)
    except Exception as exc:  # noqa: BLE001
        _log.exception("runner.loop.process_one_failed", job_id=job_id, error=str(exc))
        try:
            async with get_sessionmaker()() as session:
                j = await session.get(Job, job_id)
                image_id = j.image_id if j else ""
            await _retry_or_fail(job_id, image_id, str(exc))
        except Exception:
            pass


async def _runner_loop() -> None:
    """Parallel-dispatching runner loop.

    With one ComfyUI server: behaves like a serial worker (max 1 in flight,
    bounded by dispatcher.max_parallel_per_worker on that single worker).

    With N ComfyUI servers: at any moment we keep up to
    `dispatcher.available_slots()` jobs in flight concurrently — pulling
    the next pending Job from the in-memory queue whenever a slot opens up.
    The dispatcher's `select_worker(reserve=True)` is called inside each
    `_process_one`, which atomically picks the worker with the lowest
    in_flight count, so the load is naturally balanced across servers.

    Idle behavior: when the queue is empty AND no jobs are in flight,
    we sleep on the queue's wake event. When jobs are in flight but the
    queue is empty, we wait on the in-flight set instead so we react
    instantly when a worker frees up.
    """
    _log.info("runner.loop.starting")
    await recover_running_jobs()
    await repopulate_from_db()
    _log.info("runner.loop.ready")

    in_flight: set[asyncio.Task] = set()

    def _slots_free() -> int:
        # Dispatcher's `available_slots()` is the single source of truth — it
        # already reflects every worker.in_flight increment from
        # select_worker(reserve=True) and every decrement from
        # dispatcher.release() in the runner's finally blocks.
        #
        # We deliberately DO NOT subtract len(in_flight) here. Doing so caused
        # the runner to underdispatch in steady state: when a task hits its
        # `finally:` and calls release(), the worker's in_flight drops to 0
        # immediately — but the Python Task object itself isn't .done() yet
        # because it's still finishing post-release cleanup (job log close,
        # clear_context). So `available_slots = 2` while `len(in_flight) = 2`
        # caused us to spawn 0 new tasks, missing a slot. Over time, three
        # workers drained to one busy + two idle.
        #
        # If we accidentally over-spawn (e.g. multiple new tasks before any
        # has called select_worker yet), the surplus tasks call select_worker
        # → get None → sleep(1) → re-queue themselves. Bounded waste, no leak.
        return dispatcher.available_slots()

    while not _stop_requested:
        # PRUNE — `add_done_callback(in_flight.discard)` is scheduled via
        # `loop.call_soon` so it may not have fired yet by the time we get
        # here from `asyncio.wait(FIRST_COMPLETED)`. Compute slot availability
        # off a fresh snapshot of actually-still-running tasks. Without this,
        # the loop misses a dispatch tick after each completion — visible as
        # "after the first batch, only N-1 workers stay busy".
        in_flight = {t for t in in_flight if not t.done()}

        # Spawn as many jobs as we have free worker slots AND queued jobs.
        spawned = 0
        while _slots_free() > 0 and not _stop_requested:
            job_id = await queue.pop()
            if job_id is None:
                break
            task = asyncio.create_task(
                _process_one_safely(job_id),
                name=f"runner-job-{job_id[:8]}",
            )
            in_flight.add(task)
            task.add_done_callback(in_flight.discard)
            spawned += 1

        if spawned > 0:
            # Surface the full capacity picture so the user can see WHY only N
            # of M workers are being used (e.g. healthy=2/3, one with a recent
            # transient probe failure that hasn't reached the unhealthy threshold).
            cap = dispatcher.capacity_summary()
            _log.info(
                "runner.loop.dispatch_batch",
                spawned=spawned,
                in_flight=len(in_flight),
                free_slots=cap["free_slots"],
                healthy=cap["healthy"],
                configured=cap["configured"],
                workers=cap["workers"],
            )

        if in_flight:
            try:
                done, _pend = await asyncio.wait(
                    list(in_flight),
                    timeout=1.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except Exception as exc:  # noqa: BLE001
                _log.exception("runner.loop.wait_failed", error=str(exc))
                await asyncio.sleep(0.5)
        else:
            await queue.wait_for_jobs(timeout=2.0)

    if in_flight:
        _log.info("runner.loop.draining", in_flight=len(in_flight))
        try:
            await asyncio.wait(list(in_flight), timeout=10.0)
        except Exception:
            pass
    _log.info("runner.loop.stopped")


async def start_runner() -> None:
    """Start the background runner task. Called from FastAPI lifespan startup."""
    global _runner_task, _stop_requested
    if _runner_task is not None and not _runner_task.done():
      
        _log.warning("runner.start.already_running")
        return
    _stop_requested = False
    _runner_task = asyncio.create_task(_runner_loop(), name="robomuffin-runner")
    _log.info("runner.started")


async def stop_runner() -> None:
    """Signal the runner loop to exit and wait briefly for graceful shutdown."""
    global _runner_task, _stop_requested
    _stop_requested = True
    task = _runner_task
    if task is None:
        return
    try:
        await asyncio.wait_for(task, timeout=15.0)
    except asyncio.TimeoutError:
        _log.warning("runner.stop.timeout_cancelling")
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    finally:
        _runner_task = None
    _log.info("runner.stopped")
