"""Shared test utilities and fixtures.

Provides reusable test infrastructure: in-memory database sessions,
mock LLM clients, and factory functions for generating test data.
Pipeline-specific tests import these to avoid duplicating setup code.

Usage in conftest.py:
    from core.testing import get_test_session, MockLLMClient
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from core.llm.usage import LLMUsage

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@asynccontextmanager
async def get_test_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async session backed by an in-memory SQLite database.

    Creates all tables fresh for each test — no leftover state.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
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
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._call_index = 0
        self.usage = LLMUsage()

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        model: str | None = None,
        run_id: str = "",
    ) -> str:
        """Return the next canned response."""
        kwargs: dict[str, Any] = {
            "system": system,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "model": model,
            "run_id": run_id,
        }
        self.calls.append((prompt, kwargs))
        response = self.responses[self._call_index % len(self.responses)]
        self._call_index += 1
        return response
