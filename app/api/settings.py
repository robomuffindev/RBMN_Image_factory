"""Settings API."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.logging_setup import get_logger
from app.services import settings_service
from app.services.comfyui.dispatcher import dispatcher

router = APIRouter(prefix="/api/settings", tags=["settings"])
_log = get_logger("factory.api.settings")


class TestComfyRequest(BaseModel):
    url: str


class TestLLMRequest(BaseModel):
    provider: str = Field(pattern="^(openai|anthropic|gemini|ollama)$")
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None


class TestResult(BaseModel):
    success: bool
    message: str = ""
    detail: dict[str, Any] | None = None


def _reconfigure_dispatcher_from(row) -> None:
    """Push the latest URL list + max_parallel into the in-memory dispatcher.

    Also kicks off an async probe so health flips as soon as each server responds.
    """
    urls = row.comfyui_urls or []
    dispatcher.configure(urls, row.max_parallel_per_worker or 1)
    _log.info("settings.dispatcher.reconfigured", urls=urls, max_per_worker=row.max_parallel_per_worker)

    async def _probe_bg():
        try:
            res = await dispatcher.probe_all()
            _log.info("settings.dispatcher.probed", results={u: r.get("healthy") for u, r in res.items()})
        except Exception as exc:  # noqa: BLE001
            _log.warning("settings.dispatcher.probe_failed", error=str(exc))

    asyncio.create_task(_probe_bg())


@router.get("")
async def get_settings(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await settings_service.get_or_create(session)
    return settings_service.to_response(row)


@router.put("")
async def put_settings(payload: dict[str, Any], session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await settings_service.get_or_create(session)
    settings_service.apply_update(row, payload)
    await session.commit()
    await session.refresh(row)
    _log.info("settings.updated", keys=sorted(payload.keys()))
    # Critical: reconfigure the in-memory dispatcher so newly-added ComfyUI URLs
    # are immediately known. Without this the runner thinks 0 workers exist.
    _reconfigure_dispatcher_from(row)
    return settings_service.to_response(row)


@router.post("/test-comfyui")
async def test_comfyui(req: TestComfyRequest) -> TestResult:
    url = req.url.rstrip("/")
    verify = not ("runpod.net" in url)
    try:
        async with httpx.AsyncClient(verify=verify, timeout=10.0) as client:
            r = await client.get(f"{url}/system_stats")
            if r.status_code != 200:
                return TestResult(success=False, message=f"HTTP {r.status_code}", detail={"body": r.text[:500]})
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        return TestResult(success=True, message="OK", detail=data if isinstance(data, dict) else None)
    except Exception as exc:  # noqa: BLE001
        _log.warning("settings.test_comfyui.failed", url=url, error=str(exc))
        return TestResult(success=False, message=str(exc))


@router.post("/test-llm")
async def test_llm(req: TestLLMRequest) -> TestResult:
    provider = req.provider
    api_key = req.api_key
    model = req.model
    try:
        if provider == "openai":
            if not api_key:
                return TestResult(success=False, message="No API key provided")
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=api_key, timeout=15.0)
            await client.models.list()
            return TestResult(success=True, message="OpenAI key OK")

        if provider == "anthropic":
            if not api_key:
                return TestResult(success=False, message="No API key provided")
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=api_key, timeout=15.0)
            try:
                await client.models.list()
            except AttributeError:
                await client.messages.create(
                    model=model or "claude-haiku-4-5",
                    max_tokens=1,
                    messages=[{"role": "user", "content": "."}],
                )
            return TestResult(success=True, message="Anthropic key OK")

        if provider == "gemini":
            if not api_key:
                return TestResult(success=False, message="No API key provided")
            from google import genai
            client = genai.Client(api_key=api_key)
            await asyncio.to_thread(lambda: list(client.models.list()))
            return TestResult(success=True, message="Gemini key OK")

        if provider == "ollama":
            base_url = (req.base_url or "http://localhost:11434").rstrip("/")
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{base_url}/api/tags")
            if r.status_code != 200:
                return TestResult(success=False, message=f"HTTP {r.status_code}")
            models = [m.get("name") for m in r.json().get("models", [])]
            return TestResult(success=True, message=f"Ollama reachable ({len(models)} models)", detail={"models": models[:30]})

        return TestResult(success=False, message=f"Unknown provider: {provider}")
    except Exception as exc:  # noqa: BLE001
        _log.warning("settings.test_llm.failed", provider=provider, error=str(exc))
        return TestResult(success=False, message=str(exc))


@router.get("/builtin-prompt")
async def builtin_prompt(model: str, type: str = "image") -> dict[str, str]:
    from app.services.llm.prompts import get_builtin
    text = get_builtin(model, type)
    if text is None:
        raise HTTPException(status_code=404, detail="No built-in prompt for that model/type")
    return {"model": model, "type": type, "text": text}
