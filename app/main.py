"""FastAPI app factory + Uvicorn entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import batch as batch_api
from app.api import debug as debug_api
from app.api import downloads as downloads_api
from app.api import help as help_api
from app.api import images as images_api
from app.api import jobs as jobs_api
from app.api import llm as llm_api
from app.api import projects as projects_api
from app.api import settings as settings_api
from app.api import tools as tools_api
from app.config import PROJECT_ROOT, get_settings
from app.db.engine import dispose_engine
from app.db.migrations import run_all as run_migrations
from app.logging_setup import configure_logging, get_logger
from app.services.debug.middleware import RequestLogMiddleware
from app.services.jobs.runner import start_runner, stop_runner
from app.web import routes as web_routes


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    log = get_logger("factory.lifespan")
    log.info("app.starting", version=__version__)
    await run_migrations()

    # Configure the dispatcher from persisted settings BEFORE the runner starts.
    # If we skip this, the runner boots with zero workers known and only sees
    # them after the user re-saves the Settings page — which means jobs sit
    # forever even though servers are configured in the DB.
    try:
        from app.db.engine import get_sessionmaker
        from app.services.comfyui.dispatcher import dispatcher
        from app.services import settings_service

        async with get_sessionmaker()() as session:
            row = await settings_service.get_or_create(session)
        urls = row.comfyui_urls or []
        dispatcher.configure(urls, row.max_parallel_per_worker or 1)
        log.info("dispatcher.startup_configured", url_count=len(urls), urls=urls)
        # Fire-and-forget initial probe so health is current within seconds.
        async def _initial_probe():
            try:
                res = await dispatcher.probe_all()
                healthy = [u for u, r in res.items() if r.get("healthy")]
                unhealthy = [u for u, r in res.items() if not r.get("healthy")]
                log.info("dispatcher.startup_probed", healthy=healthy, unhealthy=unhealthy)
            except Exception as exc:  # noqa: BLE001
                log.warning("dispatcher.startup_probe_failed", error=str(exc))
        asyncio.create_task(_initial_probe(), name="dispatcher-initial-probe")

        # Periodic re-probe so a worker that was down at boot self-heals once
        # it's reachable. 30s is short enough that the user notices recovery
        # in real time and long enough to not hammer the servers.
        async def _periodic_probe():
            while True:
                await asyncio.sleep(30.0)
                try:
                    await dispatcher.probe_all()
                except Exception as exc:  # noqa: BLE001
                    log.warning("dispatcher.periodic_probe_failed", error=str(exc))
        asyncio.create_task(_periodic_probe(), name="dispatcher-periodic-probe")
    except Exception as exc:  # noqa: BLE001
        log.exception("dispatcher.startup_failed", error=str(exc))

    await start_runner()
    log.info("app.ready")
    try:
        yield
    finally:
        log.info("app.shutting_down")
        await stop_runner()
        await dispose_engine()
        log.info("app.stopped")


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    settings.ensure_dirs()

    app = FastAPI(
        title="Robomuffin Image Factory",
        version=__version__,
        lifespan=lifespan,
    )

    # --- CORS for embedding from another local app ---
    # Origins are configurable via FACTORY_CORS_ORIGINS env (comma-separated).
    # Defaults cover the common "local admin app on :8080 or Vite :5173" case.
    try:
        from fastapi.middleware.cors import CORSMiddleware
        cors_origins = settings.cors_origins_list
        if cors_origins:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=cors_origins,
                allow_credentials=True,
                allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                allow_headers=["*"],
                expose_headers=["*"],
            )
            get_logger("factory.cors").info("cors.configured", origins=cors_origins)
    except Exception as exc:  # noqa: BLE001
        get_logger("factory.cors").warning("cors.setup_failed", error=str(exc))

    # --- Optional API-key auth ---
    # When FACTORY_API_KEY is non-empty in .env, every /api/* request must
    # carry header `X-Robomuffin-Key: <value>`. Browser-side requests from
    # this app's own UI bypass auth (the middleware only gates /api/*, and
    # the UI uses same-origin fetches that include cookies). Other clients
    # (the Python SDK, the embedding app) send the header directly.
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse as _JSONResponse

    class APIKeyMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            key = (settings.factory_api_key or "").strip()
            if not key:
                return await call_next(request)
            # Only gate /api/* — leave /static, /, /projects/{id}, etc.
            # accessible so the embedded iframe / UI can load.
            path = request.url.path
            if not path.startswith("/api/"):
                return await call_next(request)
            # Allow CORS preflights through without auth so the embedding
            # app can negotiate.
            if request.method == "OPTIONS":
                return await call_next(request)
            sent = request.headers.get("X-Robomuffin-Key") or request.query_params.get("api_key", "")
            if sent != key:
                return _JSONResponse(
                    status_code=401,
                    content={"detail": "missing or invalid X-Robomuffin-Key"},
                )
            return await call_next(request)

    app.add_middleware(APIKeyMiddleware)
    app.add_middleware(RequestLogMiddleware)
    app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")
    app.include_router(batch_api.router)
    app.include_router(debug_api.router)
    app.include_router(downloads_api.router)
    app.include_router(help_api.router)
    app.include_router(images_api.images_router)
    app.include_router(images_api.projects_router)
    app.include_router(jobs_api.router)
    app.include_router(jobs_api.workers_router)
    app.include_router(jobs_api.logs_router)
    app.include_router(llm_api.router)
    app.include_router(projects_api.router)
    app.include_router(settings_api.router)
    app.include_router(tools_api.router)
    app.include_router(web_routes.router)

    @app.exception_handler(Exception)
    async def _err(_req, exc):  # noqa: ANN001
        get_logger("factory.unhandled").exception("unhandled.error", error=str(exc))
        return JSONResponse(status_code=500, content={"error": "internal", "detail": str(exc)})

    return app


if __name__ == "__main__":
    import os
    import uvicorn
    port = int(os.environ.get("FACTORY_PORT", "8765"))
    uvicorn.run("app.main:create_app", host="0.0.0.0", port=port, factory=True, reload=False, log_config=None)
