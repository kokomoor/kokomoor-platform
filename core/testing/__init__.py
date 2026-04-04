"""Shared test utilities and fixtures.

Provides reusable test infrastructure: in-memory database sessions,
mock LLM clients, and factory functions for generating test data.
Pipeline-specific tests import these to avoid duplicating setup code.

Usage in conftest.py:
    from core.testing import get_test_session, MockLLMClient
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel


@asynccontextmanager
async def get_test_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async session backed by an in-memory SQLite database.

    Creates all tables fresh for each test — no leftover state.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()
    await engine.dispose()


class MockLLMClient:
    """A mock LLM client for testing without API calls.

    Attributes:
        responses: List of canned responses to return in order.
        calls: List of (prompt, kwargs) tuples recording what was called.
    """

    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = list(responses or ["Mock LLM response."])
        self.calls: list[tuple[str, dict]] = []
        self._call_index = 0

    async def complete(self, prompt: str, **kwargs: object) -> str:
        """Return the next canned response."""
        self.calls.append((prompt, kwargs))
        response = self.responses[self._call_index % len(self.responses)]
        self._call_index += 1
        return response
