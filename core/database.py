"""Database engine and session management.

Provides an async-compatible SQLModel engine and session factory. Currently
backed by SQLite (via aiosqlite); switching to Postgres requires only a
connection-string change in ``Settings.database_url``.

Usage:
    from core.database import get_session

    async with get_session() as session:
        session.add(my_model)
        await session.commit()

The engine is constructed lazily on first use and cached for the lifetime
of the process. ``dispose_engine()`` should be called on application
shutdown so the connection pool releases its handles cleanly.
"""

from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel

from core.config import get_settings

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

# Lazy engine creation — initialised on first use.
_engine: AsyncEngine | None = None

# Modules that must be imported so their SQLModel ``table=True`` classes
# register themselves on ``SQLModel.metadata`` before ``create_all`` runs.
# Adding a new persisted model? Append its module path here.
_MODEL_MODULES: tuple[str, ...] = (
    "core.models",
    "pipelines.job_agent.models",
)


def _ensure_models_loaded() -> None:
    """Import every module that defines a persisted SQLModel.

    SQLModel only registers a class on ``SQLModel.metadata`` when its
    defining module is imported. Without this hop, ``create_all`` would
    silently emit zero CREATE TABLE statements (the bug that left the
    job_listings table missing in earlier runs).
    """
    for module_path in _MODEL_MODULES:
        importlib.import_module(module_path)


def _resolve_sqlite_path(database_url: str) -> Path | None:
    """Return the on-disk path for a sqlite URL, or None for in-memory.

    Uses SQLAlchemy's URL parser instead of string splitting so it works
    for ``sqlite://``, ``sqlite+aiosqlite://``, and absolute or relative
    paths without surprising the caller.
    """
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        return None
    database = url.database or ""
    if not database or database == ":memory:":
        return None
    return Path(database)


def _get_engine() -> AsyncEngine:
    """Create or return the cached async engine."""
    global _engine
    if _engine is None:
        settings = get_settings()

        sqlite_path = _resolve_sqlite_path(settings.database_url)
        if sqlite_path is not None:
            sqlite_path.parent.mkdir(parents=True, exist_ok=True)

        _engine = create_async_engine(
            settings.database_url,
            echo=settings.database_echo,
            future=True,
        )
    return _engine


async def init_db() -> None:
    """Create all tables defined by SQLModel metadata.

    Idempotent: ``create_all`` skips tables that already exist. Use this
    for development bootstrapping and as a safety net before tests or
    pipeline runs touch the database. In production prefer Alembic
    migrations for schema changes; this function never drops or alters
    existing tables.
    """
    _ensure_models_loaded()
    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async database session with automatic cleanup.

    Usage::

        async with get_session() as session:
            result = await session.exec(select(JobListing))
    """
    engine = _get_engine()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def dispose_engine() -> None:
    """Dispose the engine and release all connections.

    Call this during application shutdown.
    """
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
