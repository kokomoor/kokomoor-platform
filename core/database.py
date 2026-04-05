"""Database engine and session management.

Provides an async-compatible SQLModel engine and session factory. Currently
backed by SQLite (via aiosqlite); switching to Postgres requires only a
connection-string change in ``Settings.database_url``.

Usage:
    from core.database import get_session

    async with get_session() as session:
        session.add(my_model)
        await session.commit()
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from core.config import get_settings

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

# Lazy engine creation — initialised on first use.
_engine = None


def _get_engine() -> create_async_engine:
    """Create or return the cached async engine."""
    global _engine
    if _engine is None:
        settings = get_settings()

        # Ensure the SQLite data directory exists.
        if settings.database_url.startswith("sqlite"):
            db_path = settings.database_url.split("///")[-1]
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        _engine = create_async_engine(
            settings.database_url,
            echo=settings.is_dev,
            future=True,
        )
    return _engine


async def init_db() -> None:
    """Create all tables defined by SQLModel metadata.

    Use this for development bootstrapping. In production, prefer
    Alembic migrations for schema changes.
    """
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
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
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
