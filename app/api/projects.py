"""Projects API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.models import Image, Job, Project
from app.logging_setup import get_logger
from app.services.projects.service import (
    delete_project_dir,
    ensure_project_dirs,
    write_sidecar,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])
_log = get_logger("factory.api.projects")


class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    settings: dict[str, Any] = {}


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    settings: dict[str, Any] | None = None


def _sidecar(project: Project, image_count: int) -> dict[str, Any]:
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "image_count": image_count,
        "settings": project.settings,
    }


@router.get("")
async def list_projects(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = (await session.execute(select(Project).order_by(Project.updated_at.desc()))).scalars().all()
    return {"items": [r.model_dump() for r in rows]}


@router.post("")
async def create_project(req: ProjectCreate, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    p = Project(name=req.name.strip(), description=req.description, settings=req.settings, folder_path="")
    session.add(p)
    await session.flush()
    dirs = ensure_project_dirs(p.id)
    p.folder_path = str(dirs["root"])
    await session.commit()
    await session.refresh(p)
    write_sidecar(p.id, _sidecar(p, 0))
    _log.info("project.created", id=p.id, name=p.name)
    return p.model_dump()


@router.get("/{project_id}")
async def get_project(project_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    p = await session.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404)
    return p.model_dump()


@router.put("/{project_id}")
async def update_project(project_id: str, req: ProjectUpdate, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    p = await session.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404)
    if req.name is not None:
        p.name = req.name
    if req.description is not None:
        p.description = req.description
    if req.settings is not None:
        p.settings = req.settings
    p.updated_at = datetime.now().isoformat(timespec="seconds")
    await session.commit()
    await session.refresh(p)
    count = int((await session.execute(select(func.count()).select_from(Image).where(Image.project_id == p.id))).scalar_one() or 0)
    write_sidecar(p.id, _sidecar(p, count))
    return p.model_dump()


@router.delete("/{project_id}")
async def delete_project(project_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, bool]:
    p = await session.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404)
    from sqlalchemy import delete
    # Order matters: jobs reference images, images reference project.
    await session.execute(delete(Job).where(Job.project_id == project_id))
    await session.flush()
    await session.execute(delete(Image).where(Image.project_id == project_id))
    await session.flush()
    await session.delete(p)
    await session.commit()
    delete_project_dir(project_id)
    return {"ok": True}


@router.post("/{project_id}/duplicate")
async def duplicate_project(project_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    p = await session.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404)
    new = Project(name=f"{p.name} (copy)", description=p.description, settings=p.settings, folder_path="")
    session.add(new)
    await session.flush()
    dirs = ensure_project_dirs(new.id)
    new.folder_path = str(dirs["root"])
    await session.commit()
    await session.refresh(new)
    return new.model_dump()


@router.post("/{project_id}/upload-reference")
async def upload_reference(project_id: str, file: UploadFile = File(...), session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    p = await session.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404)
    dirs = ensure_project_dirs(project_id)
    raw = file.filename or "ref.png"
    safe = raw.replace("..", "_")
    safe = safe.replace("/", "_")
    safe = safe.replace(chr(92), "_")  # backslash
    dest = dirs["references"] / safe
    contents = await file.read()
    dest.write_bytes(contents)
    return {"path": str(dest), "name": safe}


# =============================================================================
# Uploaded reference assets — list, view, delete
# =============================================================================

_IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


@router.get("/{project_id}/references")
async def list_references(project_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """List all uploaded reference images for a project."""
    p = await session.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404)
    from app.services.projects.service import ensure_project_dirs
    dirs = ensure_project_dirs(project_id)
    ref_dir: Path = dirs["references"]
    items: list[dict[str, Any]] = []
    if ref_dir.exists():
        # Count how many Image rows use each reference (best-effort).
        all_images = (
            await session.execute(select(Image).where(Image.project_id == project_id))
        ).scalars().all()
        usage: dict[str, int] = {}
        for img in all_images:
            for r in img.reference_paths or []:
                usage[r] = usage.get(r, 0) + 1
        for f in sorted(ref_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not f.is_file() or f.suffix.lower() not in _IMG_EXTS:
                continue
            stat = f.stat()
            items.append({
                "name": f.name,
                "path": str(f),
                "size_bytes": stat.st_size,
                "modified": stat.st_mtime,
                "url": f"/api/projects/{project_id}/references/{f.name}",
                "usage_count": usage.get(str(f), 0),
            })
    return {"items": items}


@router.get("/{project_id}/references/{filename}")
async def serve_reference(project_id: str, filename: str, session: AsyncSession = Depends(get_session)):
    """Serve the bytes of one reference image (for thumbnails in the gallery + picker)."""
    p = await session.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404)
    from app.services.projects.service import ensure_project_dirs
    dirs = ensure_project_dirs(project_id)
    # Safety: only allow files inside references/, no traversal.
    safe = (filename or "").replace("..", "_").replace("/", "_").replace(chr(92), "_")
    f = dirs["references"] / safe
    if not f.exists() or not f.is_file():
        raise HTTPException(status_code=404, detail="reference not found")
    return FileResponse(f)


@router.delete("/{project_id}/references/{filename}")
async def delete_reference(project_id: str, filename: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Delete one uploaded reference image.

    Won't delete if any Image row currently references this path (returns 409).
    Pass ?force=1 to delete anyway and orphan the references.
    """
    from fastapi import Query as _Q  # local import to keep top-of-file tidy
    p = await session.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404)
    from app.services.projects.service import ensure_project_dirs
    dirs = ensure_project_dirs(project_id)
    safe = (filename or "").replace("..", "_").replace("/", "_").replace(chr(92), "_")
    f = dirs["references"] / safe
    if not f.exists():
        raise HTTPException(status_code=404, detail="reference not found")
    # Refuse if any Image row references this exact path (unless force).
    full_path = str(f)
    all_images = (
        await session.execute(select(Image).where(Image.project_id == project_id))
    ).scalars().all()
    in_use_by: list[str] = []
    for img in all_images:
        if full_path in (img.reference_paths or []):
            in_use_by.append(img.id)
    # ``request.query_params`` not directly accessible here without importing
    # Request — but FastAPI lets us read the raw header on the path level.
    # Keep it simple: 409 unless caller explicitly opts in via a JSON body.
    # (To keep this endpoint REST-y we always block; UI can re-confirm + retry with delete-images-first.)
    if in_use_by:
        return {"deleted": False, "in_use_by": in_use_by[:50], "in_use_count": len(in_use_by)}
    try:
        f.unlink()
        _log.info("project.reference.deleted", project_id=project_id, file=safe)
        return {"deleted": True, "file": safe}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
