"""Batch import API."""

from __future__ import annotations

import random
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.images import _enqueue_image
from app.db.engine import get_session
from app.db.models import Image, ImageStatus, Job, JobStatus, Project
from app.logging_setup import get_logger
from app.services.projects.batch_import import parse_auto
from app.services.projects.service import ensure_project_dirs, write_sidecar

router = APIRouter(prefix="/api/batch", tags=["batch"])
_log = get_logger("factory.api.batch")


@router.post("/preview")
async def preview(file: UploadFile = File(...)) -> dict[str, Any]:
    raw = (await file.read()).decode("utf-8", errors="replace")
    parsed = parse_auto(raw, filename=file.filename or "")
    return parsed.to_dict()


class CommitRow(BaseModel):
    prompt: str = ""
    negative_prompt: str = ""
    ref1: str = ""
    ref2: str = ""
    ref3: str = ""
    ref4: str = ""
    width: int | None = None
    height: int | None = None
    seed: int | str | None = None
    enhance: bool | None = None
    name: str = ""
    # Optional scale hints — get woven into the prompt by the runner so the
    # model renders the subject at correct real-world scale relative to the
    # surrounding environment. Free-form text; all three are optional.
    size: str | None = None                # e.g. "3 inches tall"
    dimensions: str | None = None          # e.g. "5x3x2 inches"
    relative_size: str | None = None       # e.g. "the size of a smartphone"


class CommitReq(BaseModel):
    mode: str  # "new" | "append"
    project_id: str | None = None
    project_name: str | None = None
    rows: list[CommitRow]


@router.post("/commit")
async def commit(req: CommitReq, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if req.mode == "new":
        if not req.project_name:
            raise HTTPException(status_code=400, detail="project_name required when mode='new'")
        project = Project(name=req.project_name.strip(), description="Imported from batch", folder_path="")
        session.add(project)
        await session.flush()
        dirs = ensure_project_dirs(project.id)
        project.folder_path = str(dirs["root"])
    elif req.mode == "append":
        if not req.project_id:
            raise HTTPException(status_code=400, detail="project_id required when mode='append'")
        project = await session.get(Project, req.project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
    else:
        raise HTTPException(status_code=400, detail="mode must be 'new' or 'append'")

    # Auto-pause the dispatcher so the imported items sit at PENDING until the
    # user clicks Start Queue — matches the Batch Render tab's behavior.
    from app.services import settings_service as _ss
    _pauserow = await _ss.get_or_create(session)
    if not getattr(_pauserow, "dispatch_paused", False):
        _pauserow.dispatch_paused = True
        await session.flush()
        _log.info("batch.commit.auto_paused_on_commit", project_id=project.id)

    queued = 0
    for r in req.rows:
        if not (r.prompt or "").strip():
            continue
        refs = [p for p in (r.ref1, r.ref2, r.ref3, r.ref4) if p][:4]
        seed: int | None = None
        if isinstance(r.seed, int):
            seed = r.seed
        elif r.seed == "random" or r.seed is None:
            seed = None
        img = Image(
            project_id=project.id,
            name=r.name or "",
            prompt=r.prompt.strip(),
            negative_prompt=(r.negative_prompt or "").strip(),
            reference_paths=refs,
            width=r.width or 1024,
            height=r.height or 1024,
            seed=seed,
            status=ImageStatus.QUEUED,
            # Scale hints — None if not supplied; trimmed so blank cells
            # don't accidentally append an empty "(, )" clause to the prompt.
            physical_size=(r.size or "").strip() or None,
            physical_dimensions=(r.dimensions or "").strip() or None,
            relative_size=(r.relative_size or "").strip() or None,
        )
        session.add(img)
        project.image_count = (project.image_count or 0) + 1
        await session.flush()
        await _enqueue_image(session, img)
        queued += 1

    _log.info("batch.commit.committing", project_id=project.id, queued=queued)
    try:
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        _log.exception("batch.commit.commit_failed", error=str(exc), queued=queued)
        await session.rollback()
        raise HTTPException(status_code=500, detail=f"batch commit failed: {exc!s}")
    await session.refresh(project)
    # Paranoid post-commit verification — counts rows actually in DB.
    from sqlalchemy import func as _func, select as _sel
    verify = await session.execute(
        _sel(_func.count()).select_from(Image).where(Image.project_id == project.id)
    )
    in_db = int(verify.scalar_one() or 0)
    _log.info("batch.commit.persisted", project_id=project.id, queued=queued, total_images_in_db=in_db)
    write_sidecar(project.id, {"id": project.id, "name": project.name, "image_count": project.image_count})
    return {"project_id": project.id, "queued": queued, "total_images_in_db": in_db}


@router.get("/example.csv", response_class=PlainTextResponse)
async def example_csv() -> PlainTextResponse:
    """Refreshed example CSV showing every supported column, including the
    three optional scale-hint columns (size / dimensions / relative_size)
    that tell the model the subject's real-world size so it renders at
    a believable scale instead of filling the frame."""
    body = (
        "prompt,ref1,ref2,ref3,ref4,width,height,seed,negative_prompt,enhance,name,size,dimensions,relative_size\n"
        '"a wide cinematic shot of a misty forest at dawn",,,,,1536,864,random,,true,forest_dawn,,,\n'
        '"a robot kitten sitting on the same forest log",refs/kitten.png,,,,1024,1024,42,,true,robokitten,"8 inches tall","8x4x3 inches","a small house cat"\n'
        '"product photo of a water spout in a modern kitchen, mounted to the counter, in correct scale",refs/spout.webp,refs/kitchen.jpg,,,1024,1024,random,"floating, oversized, dominant",true,kitchen_spout,"3 inches tall","3x2x1 inches","a coffee mug"\n'
    )
    return PlainTextResponse(body, headers={"Content-Disposition": 'attachment; filename="batch_example.csv"'})


# =============================================================================
# Batch Render — newer / richer endpoint
# =============================================================================

class BatchRenderRow(BaseModel):
    prompt: str
    negative_prompt: str = ""
    reference_paths: list[str] = []
    width: int = 1024
    height: int = 1024
    seed: int | str | None = None
    name: str = ""
    # Optional scale hints (see CommitRow above for details).
    size: str | None = None
    dimensions: str | None = None
    relative_size: str | None = None


class BatchRenderCommit(BaseModel):
    rows: list[BatchRenderRow]
    output_format: str = "png"             # png | jpg | webp
    retain_ref1_name: bool = False
    custom_output_dir: str | None = None
    enhance_prompts: bool = False          # off by default per user spec
    # If set, any reference path in the rows that is a bare filename (no \ or /)
    # will be resolved against this directory at commit time. This lets users
    # type `kitten.png` in CSVs instead of `C:\Users\hexum\refs\kitten.png` everywhere.
    reference_base_dir: str | None = None
    # Per-batch framing override: "on" | "off" | None=inherit AppSettings default.
    frame_subject: str | None = None


@router.post("/render/commit")
async def batch_render_commit(
    req: BatchRenderCommit,
    project_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Queue a Batch Render. Each row becomes one Image with the shared output options.

    Pass project_id as a query param so the same UI can target the current project
    without juggling URLs.
    """
    import uuid
    from app.api.images import _enqueue_image
    from app.services.llm.enhancer import enhance as llm_enhance
    from app.services import settings_service

    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    # Auto-pause the dispatcher BEFORE we enqueue. This way every batch commit
    # waits for the user to click "Start Queue" — preventing the "I clicked
    # Commit and it immediately started" surprise the user reported.
    s_row_pause = await settings_service.get_or_create(session)
    if not getattr(s_row_pause, "dispatch_paused", False):
        s_row_pause.dispatch_paused = True
        await session.flush()
        _log.info("batch.render.auto_paused_on_commit", project_id=project_id)

    run_id = str(uuid.uuid4())
    fmt = (req.output_format or "png").lower()
    if fmt not in ("png", "jpg", "jpeg", "webp"):
        fmt = "png"

    # Optionally enhance prompts up-front.
    enhanced_map: dict[int, str] = {}
    if req.enhance_prompts:
        s_row = await settings_service.get_or_create(session)
        import asyncio
        async def _one(i: int, p: str):
            try:
                res = await llm_enhance(p, settings_row=s_row, project_id=project_id, purpose="batch_render_enhance")
                if res.enhanced:
                    enhanced_map[i] = res.enhanced
            except Exception:
                pass  # leave original prompt
        await asyncio.gather(*(_one(i, r.prompt) for i, r in enumerate(req.rows)))

    # Resolve reference paths against the reference_base_dir, with smart fallbacks.
    # Real-world failure mode we keep hitting: user types `kitten.webp` in the CSV,
    # base_dir is `C:\stuff`, but the file actually lives at `C:\stuff\uploads\kitten.webp`.
    # The old resolver returned `C:\stuff\kitten.webp` which doesn't exist, so the
    # runner fell through and uploaded the bare filename — ComfyUI then rejects it.
    #
    # New behavior: try a series of candidate paths and return the FIRST that exists.
    # If nothing exists, return the best-guess path so the preflight log records it
    # accurately as a missing file (instead of silently sending a bare name).
    import os
    from app.config import get_settings as _gs
    base = (req.reference_base_dir or "").strip() or None
    base = base.rstrip("\\/") if base else None
    # The app's own uploads dir — useful when a user types `kitten.png` and the
    # file was uploaded via the Uploaded Assets tab.
    try:
        project_uploads = _gs().projects_dir / project_id / "uploads"
    except Exception:
        project_uploads = None
    # Global uploads dir (data/uploads) used for shared assets.
    try:
        global_uploads = _gs().data_dir / "uploads"
    except Exception:
        global_uploads = None

    unresolved: list[str] = []

    def _scrub(s: str) -> str:
        """Strip whitespace + invisible characters that sneak in from
        Excel/Notepad/CSV copy-paste (BOM, NBSP, zero-width spaces, smart quotes)
        — these silently break `os.path.isfile`."""
        if not s:
            return s
        # Trim quotes Excel sometimes adds around paths with spaces.
        s = s.strip().strip('"').strip("'").strip()
        # Remove ALL whitespace-equivalent invisibles anywhere in the string.
        for bad in ("﻿", "​", "‌", "‍", "⁠", "\xa0"):
            s = s.replace(bad, "")
        # Curly quotes → straight (defensive).
        s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
        # Trim again now that we've removed invisibles.
        return s.strip()

    def _try_paths(p: str) -> str | None:
        """Return the FIRST candidate that exists on disk, applying
        case-variant fallbacks (.WEBP vs .webp), strict resolution, and
        a directory scan when nothing matches by exact case."""
        if not p:
            return None
        try:
            if os.path.isfile(p):
                return p
        except Exception:
            pass
        # Try alternate-case extensions (Windows is usually case-insensitive
        # but bind mounts / VM file shares / OneDrive sometimes aren't).
        root, ext = os.path.splitext(p)
        if ext:
            for variant in (ext.lower(), ext.upper(), ext.capitalize()):
                cand = root + variant
                try:
                    if cand != p and os.path.isfile(cand):
                        return cand
                except Exception:
                    continue
        # Last-ditch: list the directory and compare case-insensitively.
        try:
            d = os.path.dirname(p) or "."
            name_lower = os.path.basename(p).lower()
            if os.path.isdir(d):
                for entry in os.listdir(d):
                    if entry.lower() == name_lower:
                        return os.path.join(d, entry)
        except Exception:
            pass
        return None

    def _resolve_ref(p: str) -> str:
        s = _scrub(p or "")
        if not s:
            return s

        is_abs = os.path.isabs(s) or s.startswith("\\\\") or (len(s) > 1 and s[1] == ":")
        has_sep = ("/" in s) or ("\\" in s)
        candidates: list[str] = []

        if is_abs:
            # Already absolute — try as-is, then fall back to base/<basename>
            # and base/uploads/<basename> in case the CSV came from another box.
            candidates.append(s)
            bn = os.path.basename(s)
            if base:
                candidates.append(os.path.join(base, bn))
                candidates.append(os.path.join(base, "uploads", bn))
            if project_uploads is not None:
                candidates.append(str(project_uploads / bn))
            if global_uploads is not None:
                candidates.append(str(global_uploads / bn))
        elif has_sep:
            # Path-like but relative — join to base (or just trust it).
            if base:
                candidates.append(os.path.join(base, s))
            candidates.append(s)
            bn = os.path.basename(s)
            if base:
                candidates.append(os.path.join(base, bn))
                candidates.append(os.path.join(base, "uploads", bn))
            if project_uploads is not None:
                candidates.append(str(project_uploads / bn))
        else:
            # Bare filename — full ladder.
            if base:
                candidates.append(os.path.join(base, s))
                # If user pointed base at the PARENT of an `uploads` folder.
                candidates.append(os.path.join(base, "uploads", s))
                # If user pointed base AT the uploads folder (so it's `base\\..\\uploads\\file` → same as base\\file already)
                # — covered by the first candidate. Also try parent in case base points one level too deep.
                candidates.append(os.path.join(os.path.dirname(base), s))
                candidates.append(os.path.join(os.path.dirname(base), "uploads", s))
            if project_uploads is not None:
                candidates.append(str(project_uploads / s))
            if global_uploads is not None:
                candidates.append(str(global_uploads / s))
            if not candidates:
                candidates.append(s)

        # First existing wins (with case-variant + dir-scan fallbacks).
        for c in candidates:
            hit = _try_paths(c)
            if hit:
                if hit != c:
                    _log.info("batch.ref_resolved_via_fallback", asked=c, found=hit)
                return hit
        # Nothing existed — return best-guess (first candidate) and log it
        # with a directory listing so the user can spot near-miss filenames.
        best = candidates[0] if candidates else s
        unresolved.append(best)
        nearby: list[str] = []
        try:
            d = os.path.dirname(best) or "."
            if os.path.isdir(d):
                target_stem = os.path.splitext(os.path.basename(best))[0].lower()
                for entry in os.listdir(d):
                    if target_stem and target_stem in entry.lower():
                        nearby.append(entry)
                nearby = nearby[:10]
        except Exception:
            pass
        _log.warning(
            "batch.ref_unresolved",
            original=s, best_guess=best,
            tried=candidates[:6], base=base,
            similar_files_in_dir=nearby,
        )
        return best

    queued = 0
    resolved_count = 0
    missing_count = 0
    for i, r in enumerate(req.rows):
        if not (r.prompt or "").strip():
            continue
        refs_in = [p for p in r.reference_paths if p][:4]
        refs = [_resolve_ref(p) for p in refs_in]
        # Per-row preflight log so the user can see exactly where the runner
        # will look for each reference.
        for orig, resolved in zip(refs_in, refs):
            import os as _os
            exists = bool(resolved and _os.path.isfile(resolved))
            if exists:
                resolved_count += 1
            else:
                missing_count += 1
            _log.info(
                "batch.ref_preflight",
                row=i, original=orig, resolved=resolved, exists=exists,
            )
        seed_val: int | None = None
        if isinstance(r.seed, int):
            seed_val = r.seed
        elif isinstance(r.seed, str) and r.seed.strip().isdigit():
            seed_val = int(r.seed.strip())

        # Normalize the framing field — only "on"/"off" are stored; anything
        # else means "inherit from global settings".
        _frame = (req.frame_subject or "").strip().lower()
        if _frame not in ("on", "off"):
            _frame = None
        img = Image(
            project_id=project.id,
            name=r.name or "",
            prompt=r.prompt.strip(),
            enhanced_prompt=enhanced_map.get(i),
            negative_prompt=(r.negative_prompt or "").strip(),
            reference_paths=refs,
            width=r.width or 1024,
            height=r.height or 1024,
            seed=seed_val,
            status=ImageStatus.QUEUED,
            output_format=fmt,
            retain_ref1_name=bool(req.retain_ref1_name),
            custom_output_dir=req.custom_output_dir or None,
            batch_run_id=run_id,
            frame_subject=_frame,
            # Scale hints — None if blank so the runner skips the clause builder.
            physical_size=(getattr(r, "size", None) or "").strip() or None,
            physical_dimensions=(getattr(r, "dimensions", None) or "").strip() or None,
            relative_size=(getattr(r, "relative_size", None) or "").strip() or None,
        )
        session.add(img)
        project.image_count = (project.image_count or 0) + 1
        await session.flush()
        await _enqueue_image(session, img)
        queued += 1
    # Single atomic commit for ALL images + jobs added in this batch. With the
    # flush-only _enqueue_image fix, this is the only place the transaction
    # actually persists — so any exception above this line rolls back the whole
    # batch cleanly instead of leaving half-committed rows.
    _log.info("batch.render.committing", project_id=project.id, queued=queued, run_id=run_id)
    try:
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        # If the commit itself raises, surface it loudly so we don't silently
        # return a "queued: N" response while the DB is empty.
        _log.exception("batch.render.commit_failed", error=str(exc), queued=queued)
        await session.rollback()
        raise HTTPException(status_code=500, detail=f"batch commit failed: {exc!s}")
    # Verify the rows actually landed in the DB (paranoid post-commit check —
    # this caught a session-identity bug once already).
    from sqlalchemy import func as _func, select as _sel
    verify = await session.execute(
        _sel(_func.count()).select_from(Image).where(Image.batch_run_id == run_id)
    )
    persisted = int(verify.scalar_one() or 0)
    _log.info("batch.render.persisted", run_id=run_id, queued=queued, in_db=persisted)
    if persisted != queued:
        _log.error("batch.render.persistence_mismatch", queued=queued, in_db=persisted, run_id=run_id)

    return {
        "project_id": project.id,
        "batch_run_id": run_id,
        "queued": queued,
        "in_db": persisted,
        "output_format": fmt,
        "custom_output_dir": req.custom_output_dir,
        "reference_base_dir": req.reference_base_dir,
        "enhanced_count": len(enhanced_map),
        # Preflight diagnostics — the UI surfaces these so the user can see
        # whether the references were actually located before the workers run.
        "refs_resolved": resolved_count,
        "refs_missing": missing_count,
        "refs_unresolved": unresolved[:20],   # cap so the response stays small
    }


# =============================================================================
# Batch Runs management — list, cancel, retry-failed, retry-all, clear-failed
# =============================================================================

@router.get("/runs")
async def list_batch_runs(project_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Group images by batch_run_id and return stats + actionable info per run."""
    from sqlalchemy import select as _sel
    rows = (
        await session.execute(_sel(Image).where(Image.project_id == project_id, Image.batch_run_id.is_not(None)))
    ).scalars().all()
    by_run: dict[str, dict[str, Any]] = {}
    for img in rows:
        rid = img.batch_run_id
        entry = by_run.setdefault(rid, {
            "batch_run_id": rid,
            "image_ids": [],
            "counts": {"total": 0, "queued": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0},
            "first_created_at": img.created_at,
            "last_completed_at": img.completed_at,
            "custom_output_dir": img.custom_output_dir,
            "output_format": img.output_format,
        })
        entry["image_ids"].append(img.id)
        s = img.status.value if hasattr(img.status, "value") else img.status
        entry["counts"]["total"] += 1
        if s in entry["counts"]:
            entry["counts"][s] += 1
        # earliest created
        if img.created_at and img.created_at < entry["first_created_at"]:
            entry["first_created_at"] = img.created_at
        # latest completion
        if img.completed_at:
            if not entry.get("last_completed_at") or img.completed_at > entry["last_completed_at"]:
                entry["last_completed_at"] = img.completed_at
    # Sort newest-first by first_created_at descending
    runs = sorted(by_run.values(), key=lambda r: r.get("first_created_at") or "", reverse=True)
    return {"runs": runs}


async def _images_in_run(session: AsyncSession, run_id: str, status: ImageStatus | None = None) -> list[Image]:
    from sqlalchemy import select as _sel
    q = _sel(Image).where(Image.batch_run_id == run_id)
    if status is not None:
        q = q.where(Image.status == status)
    return (await session.execute(q)).scalars().all()


@router.post("/runs/{run_id}/cancel-queued")
async def cancel_queued_in_run(run_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Mark any QUEUED images in this batch as CANCELLED. Already-RUNNING jobs are left alone."""
    imgs = await _images_in_run(session, run_id, ImageStatus.QUEUED)
    for im in imgs:
        im.status = ImageStatus.CANCELLED
        im.error = "cancelled by user"
    await session.commit()
    return {"cancelled": len(imgs), "run_id": run_id}


@router.post("/runs/{run_id}/retry-failed")
async def retry_failed_in_run(run_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Re-dispatch every FAILED image in this batch."""
    imgs = await _images_in_run(session, run_id, ImageStatus.FAILED)
    count = 0
    for im in imgs:
        im.status = ImageStatus.QUEUED
        im.error = None
        im.output_path = None
        im.thumbnail_path = None
        await session.flush()
        await _enqueue_image(session, im)
        count += 1
    await session.commit()
    return {"requeued": count, "run_id": run_id}


@router.post("/runs/{run_id}/retry-all")
async def retry_all_in_run(run_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Re-dispatch every image in this batch regardless of current status."""
    imgs = await _images_in_run(session, run_id)
    count = 0
    for im in imgs:
        im.status = ImageStatus.QUEUED
        im.error = None
        im.output_path = None
        im.thumbnail_path = None
        await session.flush()
        await _enqueue_image(session, im)
        count += 1
    await session.commit()
    return {"requeued": count, "run_id": run_id}


@router.post("/runs/{run_id}/clear-failed")
async def clear_failed_in_run(run_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Delete every FAILED image in this batch."""
    from sqlalchemy import delete as _del
    imgs = await _images_in_run(session, run_id, ImageStatus.FAILED)
    image_ids = [im.id for im in imgs]
    if image_ids:
        await session.execute(_del(Job).where(Job.image_id.in_(image_ids)))
        await session.flush()
        await session.execute(_del(Image).where(Image.id.in_(image_ids)))
        await session.commit()
    return {"deleted": len(image_ids), "run_id": run_id}


@router.delete("/runs/{run_id}")
async def delete_batch_run(run_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Delete every image in this batch (regardless of status)."""
    from sqlalchemy import delete as _del
    from pathlib import Path as _P
    imgs = await _images_in_run(session, run_id)
    image_ids = [im.id for im in imgs]
    # remove on-disk outputs best-effort
    for im in imgs:
        for f in (im.output_path, im.thumbnail_path):
            if f:
                try: _P(f).unlink()
                except Exception: pass
    if image_ids:
        await session.execute(_del(Job).where(Job.image_id.in_(image_ids)))
        await session.flush()
        await session.execute(_del(Image).where(Image.id.in_(image_ids)))
        await session.commit()
    return {"deleted": len(image_ids), "run_id": run_id}


# ---- also helper: list ALL failed images across a project (regardless of batch) ----

@router.get("/failed")
async def list_failed_images(project_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Every FAILED image in this project — for the "Failures" view."""
    from sqlalchemy import select as _sel
    imgs = (
        await session.execute(_sel(Image).where(Image.project_id == project_id, Image.status == ImageStatus.FAILED))
    ).scalars().all()
    return {"items": [{
        "id": im.id, "name": im.name, "prompt": im.prompt, "error": im.error,
        "batch_run_id": im.batch_run_id, "created_at": im.created_at,
    } for im in imgs]}


@router.post("/failed/clear")
async def clear_all_failed(project_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Delete every FAILED image in this project (any batch / no batch)."""
    from sqlalchemy import select as _sel, delete as _del
    imgs = (
        await session.execute(_sel(Image).where(Image.project_id == project_id, Image.status == ImageStatus.FAILED))
    ).scalars().all()
    image_ids = [im.id for im in imgs]
    if image_ids:
        await session.execute(_del(Job).where(Job.image_id.in_(image_ids)))
        await session.flush()
        await session.execute(_del(Image).where(Image.id.in_(image_ids)))
        await session.commit()
    return {"deleted": len(image_ids)}


# =============================================================================
# Project-wide cleanup — Clear cancelled / Purge all
# =============================================================================

@router.post("/cancelled/clear")
async def clear_all_cancelled(project_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Delete every CANCELLED image in this project, across all batches and
    standalone images. Cascades to their Job rows + best-effort removes any
    on-disk output / thumbnail file the image was associated with."""
    from sqlalchemy import select as _sel, delete as _del
    import os
    imgs = (await session.execute(
        _sel(Image).where(Image.project_id == project_id, Image.status == ImageStatus.CANCELLED)
    )).scalars().all()
    if not imgs:
        return {"deleted": 0}
    # Best-effort file cleanup BEFORE the row goes away.
    for im in imgs:
        for attr in ("output_path", "thumbnail_path"):
            p = getattr(im, attr, None)
            if p:
                try:
                    if os.path.isfile(p): os.remove(p)
                except Exception:
                    pass
    image_ids = [im.id for im in imgs]
    await session.execute(_del(Job).where(Job.image_id.in_(image_ids)))
    await session.flush()
    await session.execute(_del(Image).where(Image.id.in_(image_ids)))
    await session.commit()
    _log.warning("batch.cancelled_cleared", project_id=project_id, deleted=len(image_ids))
    return {"deleted": len(image_ids)}


@router.post("/purge-all")
async def purge_all(project_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Delete EVERY image in this project regardless of status.

    Use case: a bad CSV import that committed a hundred wrong rows, and you
    want a clean slate to try again. Also useful for clearing out a finished
    project before starting fresh work on the same Project record.

    Sequence:
      1. Drain the in-memory dispatcher queue (so no Job we're about to
         delete gets picked up mid-purge).
      2. Mark any in-flight Job rows for this project's images as CANCELLED
         so the runner exits them cleanly.
      3. Best-effort delete output / thumbnail files from disk.
      4. Cascade-delete all Jobs for this project's images.
      5. Delete every Image row in this project.
      6. Zero out the project's image_count so the gallery is empty.
    """
    from sqlalchemy import select as _sel, delete as _del, update as _upd
    from app.services.jobs.queue import queue as _queue
    import os

    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    # 1) Drain the in-memory queue.
    try:
        drained = await _queue.drain()
    except Exception as exc:  # noqa: BLE001
        drained = 0
        _log.warning("batch.purge.drain_failed", error=str(exc))

    # 2) Cancel any in-flight Jobs for this project's images.
    image_id_rows = (await session.execute(
        _sel(Image.id).where(Image.project_id == project_id)
    )).scalars().all()
    image_ids = list(image_id_rows)
    cancelled_jobs = 0
    if image_ids:
        res = await session.execute(
            _upd(Job)
            .where(Job.image_id.in_(image_ids), Job.status.in_([JobStatus.RUNNING, JobStatus.PENDING, JobStatus.RETRYING]))
            .values(status=JobStatus.CANCELLED, error="purged by user")
        )
        cancelled_jobs = res.rowcount or 0

    # 3) Disk cleanup before we delete the rows.
    imgs = (await session.execute(_sel(Image).where(Image.project_id == project_id))).scalars().all()
    file_count = 0
    for im in imgs:
        for attr in ("output_path", "thumbnail_path"):
            p = getattr(im, attr, None)
            if p:
                try:
                    if os.path.isfile(p):
                        os.remove(p)
                        file_count += 1
                except Exception:
                    pass

    # 4) Delete jobs first (FK), then images.
    if image_ids:
        await session.execute(_del(Job).where(Job.image_id.in_(image_ids)))
        await session.flush()
        await session.execute(_del(Image).where(Image.id.in_(image_ids)))

    if project is not None:
        project.image_count = 0

    await session.commit()
    _log.warning(
        "batch.purge_all",
        project_id=project_id,
        images_deleted=len(image_ids),
        files_removed=file_count,
        queue_drained=drained,
        in_flight_jobs_cancelled=cancelled_jobs,
    )
    return {
        "deleted": len(image_ids),
        "files_removed": file_count,
        "queue_drained": drained,
        "in_flight_jobs_cancelled": cancelled_jobs,
    }
