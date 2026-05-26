"""Images API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.models import Image, ImageStatus, Job, JobStatus, Project
from app.logging_setup import get_logger
from app.services.jobs.queue import queue
from app.web.routes import templates

projects_router = APIRouter(prefix="/api/projects", tags=["images"])
images_router = APIRouter(prefix="/api/images", tags=["images"])
_log = get_logger("factory.api.images")


class ImageCreate(BaseModel):
    name: str = ""
    prompt: str
    enhanced_prompt: str | None = None
    negative_prompt: str = ""
    reference_paths: list[str] = []
    width: int = 1024
    height: int = 1024
    seed: int | None = None
    dispatch: bool = True
    parameters: dict[str, Any] = {}
    # Output controls (Batch Render).
    output_format: str = "png"             # png | jpg | webp
    retain_ref1_name: bool = False
    custom_output_dir: str | None = None
    batch_run_id: str | None = None


def _serialize_image(img: Image) -> dict[str, Any]:
    # Per-reference existence flag so the gallery can show missing files in red.
    import os as _os
    refs = list(img.reference_paths or [])
    references_detail = [
        {"path": p, "exists": bool(p and _os.path.isfile(p)), "name": _os.path.basename(p) if p else ""}
        for p in refs
    ]
    return {
        "id": img.id,
        "project_id": img.project_id,
        "order_index": img.order_index,
        "name": img.name,
        "prompt": img.prompt,
        "enhanced_prompt": img.enhanced_prompt,
        "negative_prompt": img.negative_prompt,
        "reference_paths": refs,
        "references_detail": references_detail,
        "width": img.width,
        "height": img.height,
        "seed": img.seed,
        "status": img.status.value if hasattr(img.status, "value") else img.status,
        "output_path": img.output_path,
        "thumbnail_path": img.thumbnail_path,
        "output_format": img.output_format,
        "retain_ref1_name": img.retain_ref1_name,
        "custom_output_dir": img.custom_output_dir,
        "batch_run_id": img.batch_run_id,
        "error": img.error,
        "created_at": img.created_at,
        "completed_at": img.completed_at,
        "parameters": img.parameters,
    }


@projects_router.get("/{project_id}/images")
async def list_images(project_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    # Sanity-log every list call so we can see "refresh returns 0 rows" failures
    # in factory.log without DB introspection.
    rows = (
        await session.execute(
            select(Image).where(Image.project_id == project_id).order_by(Image.created_at.desc())
        )
    ).scalars().all()
    proj = await session.get(Project, project_id)
    proj_count_field = (proj.image_count if proj is not None else None)
    _log.info(
        "images.list",
        project_id=project_id,
        returned=len(rows),
        project_exists=(proj is not None),
        project_image_count_field=proj_count_field,
    )
    return {"items": [_serialize_image(r) for r in rows]}


@projects_router.post("/{project_id}/images")
async def create_image(project_id: str, req: ImageCreate, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404)
    img = Image(
        project_id=project_id,
        name=req.name,
        prompt=req.prompt,
        enhanced_prompt=req.enhanced_prompt,
        negative_prompt=req.negative_prompt,
        reference_paths=[p for p in req.reference_paths if p][:4],
        width=req.width,
        height=req.height,
        seed=req.seed,
        parameters=req.parameters,
        status=ImageStatus.QUEUED,
        output_format=req.output_format or "png",
        retain_ref1_name=bool(req.retain_ref1_name),
        custom_output_dir=req.custom_output_dir or None,
        batch_run_id=req.batch_run_id or None,
    )
    session.add(img)
    project.image_count = (project.image_count or 0) + 1
    # Flush so img.id is populated for _enqueue_image's Job FK, but do NOT
    # commit yet — single end-of-function commit keeps Image + Job atomic.
    await session.flush()
    if req.dispatch:
        await _enqueue_image(session, img)
    await session.commit()
    await session.refresh(img)
    return _serialize_image(img)


async def _enqueue_image(session: AsyncSession, img: Image, priority: int = 0) -> Job:
    """Create a Job row for the image and (unless paused) place it on the
    in-memory dispatcher queue.

    IMPORTANT: this function calls ``session.flush()`` (NOT ``commit()``).
    The caller is responsible for ``await session.commit()`` before the
    session closes — otherwise the Job (and any pending Image/Project
    mutations) will be rolled back.

    Why: this function is called inside batch loops that add many Image rows
    before a single end-of-loop commit. A premature commit inside this helper
    corrupted that pattern — it'd commit some rows mid-loop, leave SQLAlchemy
    identity-map state inconsistent, and (in the worst case) cause the
    user's whole batch to vanish on the next page reload.

    The in-memory ``queue.enqueue`` is still called here because the runner's
    ``_process_one`` is defensive: if it pops a job_id that doesn't yet exist
    in the DB (transient race while caller is still committing) it returns
    cleanly — no harm done. So we keep "enqueue at flush time, commit at
    end" for liveness, while preserving transactional atomicity.
    """
    job = Job(
        image_id=img.id,
        project_id=img.project_id,
        status=JobStatus.PENDING,
        priority=priority,
        parameters={"width": img.width, "height": img.height, "seed": img.seed},
    )
    session.add(job)
    # flush populates job.id without committing the surrounding transaction.
    await session.flush()
    # Pause check is read-only on AppSettings — no commits inside.
    paused = False
    try:
        from app.services import settings_service
        s_row = await settings_service.get_or_create(session)
        paused = bool(getattr(s_row, "dispatch_paused", False))
    except Exception:
        paused = False
    if not paused:
        await queue.enqueue(job.id, priority)
    return job


@images_router.get("/{image_id}")
async def get_image(image_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    img = await session.get(Image, image_id)
    if img is None:
        raise HTTPException(status_code=404)
    return _serialize_image(img)


class ImagePatch(BaseModel):
    prompt: str | None = None
    enhanced_prompt: str | None = None
    negative_prompt: str | None = None
    reference_paths: list[str] | None = None
    width: int | None = None
    height: int | None = None
    seed: int | None = None


@images_router.put("/{image_id}")
async def update_image(image_id: str, req: ImagePatch, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    img = await session.get(Image, image_id)
    if img is None:
        raise HTTPException(status_code=404)
    for k, v in req.model_dump(exclude_unset=True).items():
        setattr(img, k, v)
    await session.commit()
    await session.refresh(img)
    return _serialize_image(img)


@images_router.delete("/{image_id}", response_class=HTMLResponse)
async def delete_image(image_id: str, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    img = await session.get(Image, image_id)
    if img is None:
        raise HTTPException(status_code=404)
    project = await session.get(Project, img.project_id)
    if img.output_path and Path(img.output_path).exists():
        try:
            Path(img.output_path).unlink()
        except Exception:
            pass
    if img.thumbnail_path and Path(img.thumbnail_path).exists():
        try:
            Path(img.thumbnail_path).unlink()
        except Exception:
            pass
    # Delete jobs first and FLUSH so the FK constraint sees them gone
    # before we delete the image.
    from sqlalchemy import delete
    await session.execute(delete(Job).where(Job.image_id == image_id))
    await session.flush()
    await session.delete(img)
    if project and project.image_count > 0:
        project.image_count -= 1
    await session.commit()
    return HTMLResponse("")


@images_router.post("/{image_id}/dispatch")
async def dispatch_image(image_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    img = await session.get(Image, image_id)
    if img is None:
        raise HTTPException(status_code=404)
    img.status = ImageStatus.QUEUED
    img.error = None
    img.output_path = None
    img.thumbnail_path = None
    await session.flush()
    job = await _enqueue_image(session, img)
    await session.commit()
    return {"job_id": job.id, "image_id": img.id, "status": "queued"}


class RerunBody(BaseModel):
    """Body for /rerun — every field is optional and ONLY overrides the
    existing image if provided. Lets the user iterate on one image without
    cluttering the project with throwaway rows."""
    prompt: str | None = None
    negative_prompt: str | None = None
    reference_paths: list[str] | None = None
    # Seed handling — pick one of: keep the previous seed, randomize, or
    # use the integer the user typed in. "same" / "random" / int / null.
    seed_mode: str | int | None = None


@images_router.post("/{image_id}/rerun")
async def rerun_image(
    image_id: str,
    req: RerunBody,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Re-dispatch an image, optionally with edited prompt / refs / seed.

    Overwrites the existing image row (same id, same project, same batch_run_id)
    rather than spawning a duplicate — this is the "I want to iterate on THIS
    image until it's right" workflow. Deletes the old output file + thumbnail
    from disk so the next render replaces them cleanly.
    """
    img = await session.get(Image, image_id)
    if img is None:
        raise HTTPException(status_code=404)
    if req.prompt is not None and req.prompt.strip():
        img.prompt = req.prompt.strip()
        img.enhanced_prompt = None
    if req.negative_prompt is not None:
        img.negative_prompt = req.negative_prompt.strip()
    if req.reference_paths is not None:
        img.reference_paths = [p for p in req.reference_paths if p][:4]
    sm = req.seed_mode
    if sm is None or sm == "random":
        img.seed = None
    elif sm == "same":
        pass
    else:
        try:
            img.seed = int(sm)
        except (TypeError, ValueError):
            img.seed = None
    import os as _os
    for attr in ("output_path", "thumbnail_path"):
        p = getattr(img, attr, None)
        if p:
            try:
                if _os.path.isfile(p):
                    _os.remove(p)
            except Exception:
                pass
    img.output_path = None
    img.thumbnail_path = None
    img.error = None
    img.status = ImageStatus.QUEUED
    img.completed_at = None
    await session.flush()
    job = await _enqueue_image(session, img)
    await session.commit()
    await session.refresh(img)
    _log.info(
        "image.rerun_queued",
        image_id=img.id, job_id=job.id,
        seed=img.seed, refs=len(img.reference_paths or []),
        prompt_len=len((img.prompt or "")),
    )
    return {
        "job_id": job.id,
        "image_id": img.id,
        "status": "queued",
        "seed": img.seed,
        "reference_paths": img.reference_paths,
        "prompt": img.prompt,
    }



@images_router.get("/{image_id}/file")
async def serve_image_file(image_id: str, download: int = 0, session: AsyncSession = Depends(get_session)) -> FileResponse:
    """Serve the rendered output file for an image.

    Diagnostics: logs `images.serve.missing_path` when the row has no
    output_path (job not done yet) and `images.serve.file_not_found` when
    the DB says there's a file but it's gone from disk. The browser's
    img.onerror handler in the UI surfaces these distinctly so you can tell
    "still rendering" from "file got deleted / never written".
    """
    import mimetypes, os as _os
    img = await session.get(Image, image_id)
    if img is None:
        _log.warning("images.serve.image_not_found", image_id=image_id)
        raise HTTPException(status_code=404, detail="image row not found")
    if not img.output_path:
        _log.info("images.serve.missing_path", image_id=image_id, status=str(img.status))
        raise HTTPException(status_code=404, detail="image has no output yet")
    path = Path(img.output_path)
    if not path.exists():
        _log.warning("images.serve.file_not_found", image_id=image_id, output_path=img.output_path)
        raise HTTPException(status_code=404, detail=f"file not on disk: {img.output_path}")
    # Force a correct media_type — Starlette's FileResponse uses mimetypes.guess_type
    # which is sometimes wrong on Windows for .webp. We set it explicitly.
    media_type, _ = mimetypes.guess_type(str(path))
    if not media_type:
        ext = path.suffix.lower()
        media_type = {".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg",
                      ".jpeg": "image/jpeg", ".gif": "image/gif"}.get(ext, "application/octet-stream")
    headers: dict[str, str] = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
    return FileResponse(path, headers=headers, media_type=media_type)


@images_router.get("/{image_id}/thumb")
async def serve_image_thumb(image_id: str, session: AsyncSession = Depends(get_session)) -> FileResponse:
    img = await session.get(Image, image_id)
    if img is None or not img.thumbnail_path:
        raise HTTPException(status_code=404)
    path = Path(img.thumbnail_path)
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/jpeg")


@images_router.get("/{image_id}/card", response_class=HTMLResponse)
async def image_card(image_id: str, request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    img = await session.get(Image, image_id)
    if img is None:
        return HTMLResponse("", status_code=200)
    return templates.TemplateResponse(request, "partials/image_card.html", {"img": img})
