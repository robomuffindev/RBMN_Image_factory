"""Server-rendered HTML pages.

Bound to root paths (no /api prefix) so users hit them directly:
    /                       → projects list (Phase 5+)
    /projects/{id}          → project view with tabs
    /settings               → settings page
    /help                   → help page (Phase 10)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.config import PROJECT_ROOT
from app.db.engine import get_session
from app.db.models import Image, Project
from app.services import settings_service

templates = Jinja2Templates(directory=str(PROJECT_ROOT / "app" / "web" / "templates"))

router = APIRouter(tags=["web"])


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    projects = (await session.execute(select(Project).order_by(Project.updated_at.desc()))).scalars().all()
    return templates.TemplateResponse(
        request,
        "projects_list.html",
        {"projects": projects, "version": __version__},
    )


@router.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_view(
    request: Request,
    project_id: str,
    tab: str = "gallery",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    project = await session.get(Project, project_id)
    if project is None:
        return RedirectResponse(url="/", status_code=303)
    images = (
        (await session.execute(select(Image).where(Image.project_id == project_id).order_by(Image.order_index))).scalars().all()
    )
    return templates.TemplateResponse(
        request,
        "project_view.html",
        {
            "project": project,
            "images": images,
            "tab": tab,
            "version": __version__,
        },
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    row = await settings_service.get_or_create(session)
    settings = settings_service.to_response(row)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"settings": settings, "version": __version__},
    )


@router.get("/help", response_class=HTMLResponse)
async def help_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "help.html", {"version": __version__})
