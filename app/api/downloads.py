"""Project download endpoints."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.models import Image, Project
from app.services.downloads.zip import stream_project_zip

router = APIRouter(prefix="/api/projects", tags=["downloads"])

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@router.get("/{project_id}/zip")
async def download_project_zip(
    project_id: str,
    include_prompts: int = 1,
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404)
    images = (
        await session.execute(select(Image).where(Image.project_id == project_id).order_by(Image.created_at))
    ).scalars().all()

    records = []
    for img in images:
        if not img.output_path:
            continue
        records.append({
            "output_path": img.output_path,
            "name": img.name,
            "prompt": img.prompt,
            "enhanced_prompt": img.enhanced_prompt,
            "width": img.width,
            "height": img.height,
            "seed": img.seed,
        })
    safe_name = _SAFE.sub("_", project.name) or "project"
    headers = {"Content-Disposition": f'attachment; filename="{safe_name}.zip"'}
    return StreamingResponse(
        stream_project_zip(safe_name, records, include_prompts_csv=bool(include_prompts)),
        media_type="application/zip",
        headers=headers,
    )
