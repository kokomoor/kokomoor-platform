"""Tests for DiscoveryOrchestrator auth and sequencing behavior."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from pipelines.job_agent.discovery.models import DiscoveryConfig, ListingRef, ProviderResult
from pipelines.job_agent.discovery.orchestrator import DiscoveryOrchestrator
from pipelines.job_agent.discovery.providers.base import BaseProvider
from pipelines.job_agent.models import JobSource, SearchCriteria


class _AuthProvider(BaseProvider):
    source = JobSource.WELLFOUND

    def requires_auth(self) -> bool:
        return True

    def base_domain(self) -> str:
        return "wellfound.com"

    async def is_authenticated(self, page: object) -> bool:
        return False

    async def authenticate(
        self, page: object, *, email: str, password: str, behavior: object
    ) -> bool:
        return True

    def _build_search_urls(self, criteria: SearchCriteria, config: DiscoveryConfig) -> list[str]:
        return []

    async def _extract_refs_from_page(self, page: object) -> list[ListingRef]:
        return []


@pytest.mark.asyncio
async def test_auth_provider_missing_credentials_returns_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _AuthProvider()
    criteria = SearchCriteria()
    config = DiscoveryConfig(sessions_dir="/tmp/sessions")
    settings = MagicMock(
        linkedin_email="",
        linkedin_password=SecretStr(""),
        wellfound_email="",
        wellfound_password=SecretStr(""),
    )
    session_store = MagicMock()
    session_store.load.return_value = None
    session_store.save = AsyncMock(return_value=True)
    semaphore = asyncio.Semaphore(1)

    fake_page = AsyncMock()
    fake_page.goto = AsyncMock()

    class _FakeBrowserManager:
        async def __aenter__(self) -> _FakeBrowserManager:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def new_page(self) -> AsyncMock:
            return fake_page

    monkeypatch.setattr(
        "pipelines.job_agent.discovery.orchestrator.BrowserManager",
        lambda storage_state=None: _FakeBrowserManager(),
    )

    result = await DiscoveryOrchestrator._run_browser_provider(
        provider,
        criteria,
        config,
        settings,
        session_store,
        semaphore,
    )

    assert isinstance(result, ProviderResult)
    assert result.errors == ["auth_missing_credentials"]


@pytest.mark.asyncio
async def test_first_navigation_wait_happens_before_warmup_goto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _AuthProvider()
    criteria = SearchCriteria()
    config = DiscoveryConfig(sessions_dir="/tmp/sessions")
    settings = MagicMock(
        linkedin_email="",
        linkedin_password=SecretStr(""),
        wellfound_email="user@example.com",
        wellfound_password=SecretStr("secret"),
    )
    session_store = MagicMock()
    session_store.load.return_value = None
    session_store.save = AsyncMock(return_value=True)
    semaphore = asyncio.Semaphore(1)
    events: list[str] = []

    fake_page = AsyncMock()

    async def _goto(*args: object, **kwargs: object) -> None:
        events.append("goto")

    fake_page.goto = AsyncMock(side_effect=_goto)

    class _FakeBrowserManager:
        async def __aenter__(self) -> _FakeBrowserManager:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def new_page(self) -> AsyncMock:
            return fake_page

    class _FakeRateLimiter:
        def __init__(self, source: JobSource) -> None:
            self.page_count = 0

        async def wait(self) -> None:
            events.append("wait")

    monkeypatch.setattr(
        "pipelines.job_agent.discovery.orchestrator.BrowserManager",
        lambda storage_state=None: _FakeBrowserManager(),
    )
    monkeypatch.setattr(
        "pipelines.job_agent.discovery.orchestrator.DomainRateLimiter",
        _FakeRateLimiter,
    )

    result = await DiscoveryOrchestrator._run_browser_provider(
        provider,
        criteria,
        config,
        settings,
        session_store,
        semaphore,
    )

    assert isinstance(result, ProviderResult)
    assert events.index("wait") < events.index("goto")
