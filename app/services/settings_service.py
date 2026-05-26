"""Helpers around the AppSettings singleton."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppSettings
from app.logging_setup import get_logger

_log = get_logger("factory.settings")

MASK_TOKEN = "********"


def mask_secret(value: str | None) -> str | None:
    """Return ``****abcd`` (last 4 chars) for storage in API responses."""
    if not value:
        return value
    if len(value) <= 4:
        return MASK_TOKEN
    return f"{MASK_TOKEN}{value[-4:]}"


def is_masked(value: str | None) -> bool:
    """Detect ``****abcd``-shaped strings sent back by the form."""
    if value is None:
        return False
    return value.startswith(MASK_TOKEN)


async def get_or_create(session: AsyncSession) -> AppSettings:
    row = await session.get(AppSettings, 1)
    if row is None:
        _log.warning("settings.missing.creating_default")
        row = AppSettings(id=1)
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


def to_response(row: AppSettings) -> dict[str, Any]:
    """Serialize for the API (with secrets masked)."""
    return {
        "comfyui_urls": row.comfyui_urls or [],
        "comfyui_server_caps": row.comfyui_server_caps or {},
        "openai_api_key": mask_secret(row.openai_api_key),
        "openai_model": row.openai_model,
        "anthropic_api_key": mask_secret(row.anthropic_api_key),
        "anthropic_model": row.anthropic_model,
        "gemini_api_key": mask_secret(row.gemini_api_key),
        "gemini_model": row.gemini_model,
        "ollama_base_url": row.ollama_base_url,
        "ollama_model": row.ollama_model,
        "default_llm_provider": row.default_llm_provider,
        "image_model_type": row.image_model_type,
        "image_system_prompt_overrides": row.image_system_prompt_overrides or {},
        "image_prompt_guidance": row.image_prompt_guidance or {},
        "default_width": row.default_width,
        "default_height": row.default_height,
        "default_seed_mode": row.default_seed_mode,
        "max_parallel_per_worker": row.max_parallel_per_worker,
        "restrict_explicit_content": row.restrict_explicit_content,
        "project_dir": row.project_dir,
        "use_gpt_image": row.use_gpt_image,
        "gpt_image_model": row.gpt_image_model,
        "gpt_image_size": row.gpt_image_size,
        "gpt_image_quality": row.gpt_image_quality,
        "dispatch_paused": getattr(row, "dispatch_paused", False),
        "frame_subject_default": getattr(row, "frame_subject_default", False),
        "frame_subject_strength": getattr(row, "frame_subject_strength", "moderate"),
        "frame_subject_positive_override": getattr(row, "frame_subject_positive_override", None),
        "frame_subject_negative_override": getattr(row, "frame_subject_negative_override", None),
        "updated_at": row.updated_at,
    }


def apply_update(row: AppSettings, payload: dict[str, Any]) -> AppSettings:
    """Patch updatable fields. Masked secret values are ignored.

    The caller is responsible for ``session.commit()``.
    """
    # Plain scalar fields ----------------------------------------------------
    scalars = [
        "openai_model",
        "anthropic_model",
        "gemini_model",
        "ollama_base_url",
        "ollama_model",
        "default_llm_provider",
        "image_model_type",
        "default_width",
        "default_height",
        "default_seed_mode",
        "max_parallel_per_worker",
        "restrict_explicit_content",
        "project_dir",
        "use_gpt_image",
        "gpt_image_model",
        "gpt_image_size",
        "gpt_image_quality",
        "dispatch_paused",
        "frame_subject_default",
        "frame_subject_strength",
    ]
    for key in scalars:
        if key in payload and payload[key] is not None:
            setattr(row, key, payload[key])

    # Framing overrides — empty string is valid (means "use preset"), so we
    # accept None or string, only skipping if the key is absent from payload.
    for key in ("frame_subject_positive_override", "frame_subject_negative_override"):
        if key in payload:
            v = payload[key]
            setattr(row, key, (v.strip() if isinstance(v, str) and v.strip() else None))

    # Secret API keys — accept new values, ignore masked echoes from the UI.
    for key in ("openai_api_key", "anthropic_api_key", "gemini_api_key"):
        if key in payload and not is_masked(payload[key]):
            setattr(row, key, payload[key] or None)

    # ComfyUI URL list — strip, deduplicate (preserving order), and drop blanks.
    # This was previously missing, so users saw their new URLs disappear on save.
    if "comfyui_urls" in payload and isinstance(payload["comfyui_urls"], list):
        seen: set[str] = set()
        clean: list[str] = []
        for raw in payload["comfyui_urls"]:
            if not isinstance(raw, str):
                continue
            u = raw.strip().rstrip("/")
            if not u:
                continue
            if u in seen:
                continue
            seen.add(u)
            clean.append(u)
        row.comfyui_urls = clean
        _log.info("settings.comfyui_urls.saved", count=len(clean), urls=clean)

    # JSON dict fields — merge/replace if provided.
    for key in ("image_system_prompt_overrides", "image_prompt_guidance", "comfyui_server_caps"):
        if key in payload and isinstance(payload[key], dict):
            setattr(row, key, payload[key])

    from datetime import datetime

    row.updated_at = datetime.now().isoformat(timespec="seconds")
    return row
