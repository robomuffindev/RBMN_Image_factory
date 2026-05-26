"""LLM enhancement API."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.models import Image
from app.logging_setup import get_logger
from app.services import settings_service
from app.services.llm.enhancer import enhance

router = APIRouter(prefix="/api/llm", tags=["llm"])
_log = get_logger("factory.api.llm")


class EnhanceReq(BaseModel):
    prompt: str
    context: str = ""
    provider: str | None = None
    project_id: str | None = None
    image_id: str | None = None


class EnhanceResp(BaseModel):
    enhanced: str
    provider: str
    model: str
    tokens_input: int | None = None
    tokens_output: int | None = None
    latency_ms: float | None = None


@router.post("/enhance", response_model=EnhanceResp)
async def enhance_single(req: EnhanceReq, session: AsyncSession = Depends(get_session)) -> EnhanceResp:
    s = await settings_service.get_or_create(session)
    try:
        res = await enhance(
            req.prompt,
            settings_row=s,
            context=req.context,
            provider_override=req.provider,
            project_id=req.project_id,
            image_id=req.image_id,
        )
    except Exception as exc:  # noqa: BLE001
        _log.exception("llm.enhance.failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))
    return EnhanceResp(**res.__dict__)


class EnhanceBulkReq(BaseModel):
    prompts: list[str]
    context: str = ""
    provider: str | None = None
    project_id: str | None = None


@router.post("/enhance-bulk")
async def enhance_bulk(req: EnhanceBulkReq, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    s = await settings_service.get_or_create(session)

    async def _one(p: str) -> dict[str, Any]:
        try:
            res = await enhance(p, settings_row=s, context=req.context, provider_override=req.provider, project_id=req.project_id)
            return {"enhanced": res.enhanced, "provider": res.provider, "model": res.model, "latency_ms": res.latency_ms}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "enhanced": None}

    # Cap concurrency to avoid rate limits.
    sem = asyncio.Semaphore(4)

    async def _bounded(p: str) -> dict[str, Any]:
        async with sem:
            return await _one(p)

    results = await asyncio.gather(*(_bounded(p) for p in req.prompts))
    return {"results": results}


class EnhanceProjectReq(BaseModel):
    provider: str | None = None
    only_missing: bool = True


@router.post("/projects/{project_id}/enhance-all")
async def enhance_project(project_id: str, req: EnhanceProjectReq, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Enhance enhanced_prompt for every image in a project."""
    s = await settings_service.get_or_create(session)
    rows = (await session.execute(select(Image).where(Image.project_id == project_id))).scalars().all()

    sem = asyncio.Semaphore(4)
    updated = 0

    async def _one(img: Image) -> None:
        nonlocal updated
        if req.only_missing and img.enhanced_prompt:
            return
        async with sem:
            try:
                res = await enhance(img.prompt, settings_row=s, provider_override=req.provider, project_id=project_id, image_id=img.id)
                img.enhanced_prompt = res.enhanced
                updated += 1
            except Exception as exc:  # noqa: BLE001
                _log.warning("llm.enhance.project.image_failed", image_id=img.id, error=str(exc))

    await asyncio.gather(*(_one(img) for img in rows))
    await session.commit()
    return {"updated": updated, "total": len(rows)}
