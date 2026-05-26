"""Async SQLite engine."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings
from app.logging_setup import get_logger

_log = get_logger("factory.db")
_engine = None
_SessionLocal: async_sessionmaker[AsyncSession] | None = None


def _build_engine():
    settings = get_settings()
    settings.ensure_dirs()
    url = f"sqlite+aiosqlite:///{settings.db_path.as_posix()}"
    _log.info("db.engine.create", url=url)
    engine = create_async_engine(
        url,
        echo=False,
        future=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        for pragma in (
            "PRAGMA journal_mode=WAL;",
            "PRAGMA synchronous=NORMAL;",
            "PRAGMA foreign_keys=ON;",
            "PRAGMA busy_timeout=5000;",
        ):
            try:
                cur.execute(pragma)
            except Exception as exc:  # noqa: BLE001
                _log.warning("db.pragma.failed", pragma=pragma.strip(), error=str(exc))
        cur.close()

    return engine


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        _engine = _build_engine()
        _SessionLocal = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _SessionLocal = None
        _log.info("db.engine.disposed")
