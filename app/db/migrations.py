"""Tiny in-process migration runner.

For v0.1 we don't have schema changes yet — ``create_all`` is enough.
Future migrations should append to ``_MIGRATIONS`` and check a
``schema_version`` row.
"""

from __future__ import annotations

import asyncio

from sqlmodel import SQLModel, select

from app.config import get_settings
from app.db import models as _models  # noqa: F401 — registers tables on the metadata
from app.db.engine import dispose_engine, get_engine, get_sessionmaker
from app.db.models import AppSettings
from app.logging_setup import configure_logging, get_logger


async def create_all() -> None:
    """Create tables if they don't exist + apply additive ALTER TABLEs."""
    log = get_logger("factory.migrations")
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    # Additive column migrations for users whose DB pre-dates a new field.
    # SQLite ADD COLUMN is forgiving; we ignore "duplicate column" errors.
    from sqlalchemy import text
    additive_columns = [
        # (table, column_name, sql_type, default)
        ("image", "output_format",     "TEXT",     "'png'"),
        ("image", "retain_ref1_name",  "INTEGER",  "0"),
        ("image", "custom_output_dir", "TEXT",     "NULL"),
        ("image", "batch_run_id",      "TEXT",     "NULL"),
        ("app_settings", "use_gpt_image",    "INTEGER", "0"),
        ("app_settings", "gpt_image_model",  "TEXT",    "'gpt-image-2'"),
        ("app_settings", "gpt_image_size",   "TEXT",    "'auto'"),
        ("app_settings", "gpt_image_quality","TEXT",    "'auto'"),
        ("app_settings", "dispatch_paused",  "INTEGER", "0"),
        ("app_settings", "frame_subject_default",            "INTEGER", "0"),
        ("app_settings", "frame_subject_strength",           "TEXT",    "'moderate'"),
        ("app_settings", "frame_subject_positive_override",  "TEXT",    "NULL"),
        ("app_settings", "frame_subject_negative_override",  "TEXT",    "NULL"),
        ("image",        "frame_subject",                    "TEXT",    "NULL"),
    ]
    async with engine.begin() as conn:
        for table, col, typ, default in additive_columns:
            try:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {typ} DEFAULT {default}"))
                log.info("db.alter.added_column", table=table, column=col)
            except Exception as exc:
                # SQLite raises "duplicate column" if already present — that's the happy case.
                msg = str(exc).lower()
                if "duplicate column" in msg or "already exists" in msg:
                    continue
                log.warning("db.alter.failed", table=table, column=col, error=str(exc))
    log.info("db.create_all.done")


async def seed_app_settings() -> None:
    """Insert the singleton AppSettings(id=1) row on first run."""
    log = get_logger("factory.migrations")
    settings = get_settings()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as session:
        existing = await session.get(AppSettings, 1)
        if existing is not None:
            log.info("db.seed.skip", reason="app_settings already exists")
            return
        row = AppSettings(
            id=1,
            comfyui_urls=settings.comfyui_urls_list(),
            openai_api_key=settings.openai_api_key or None,
            openai_model=settings.openai_model or None,
            anthropic_api_key=settings.anthropic_api_key or None,
            anthropic_model=settings.anthropic_model or None,
            gemini_api_key=settings.gemini_api_key or None,
            gemini_model=settings.gemini_model or None,
            ollama_base_url=settings.ollama_base_url or None,
            ollama_model=settings.ollama_model or None,
            default_llm_provider=settings.default_llm_provider or None,
            default_width=settings.default_image_width,
            default_height=settings.default_image_height,
            default_seed_mode=settings.default_seed_mode,
            max_parallel_per_worker=settings.max_parallel_per_worker,
            restrict_explicit_content=settings.restrict_explicit_content,
        )
        session.add(row)
        await session.commit()
        log.info("db.seed.app_settings", comfyui_urls=row.comfyui_urls)


async def run_all() -> None:
    """Idempotent migration runner — call from app.main on startup."""
    log = get_logger("factory.migrations")
    log.info("db.create_all.start")
    await create_all()
    await seed_app_settings()
