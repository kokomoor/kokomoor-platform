"""Tests for DiscoveryOrchestrator auth, retry, and sequencing behavior."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from core.browser.debug_capture import FailureCapture
from pipelines.job_agent.discovery.models import DiscoveryConfig, ListingRef, ProviderResult
from pipelines.job_agent.discovery.orchestrator import DiscoveryOrchestrator
from pipelines.job_agent.discovery.providers.base import BaseProvider
from pipelines.job_agent.models import JobSource, SearchCriteria


def _make_capture() -> FailureCapture:
    return FailureCapture(
        enabled=False,
        base_dir="/tmp/debug",
        run_id="test-run",
        include_html=False,
    )


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
    session_store.invalidate = MagicMock()

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

    result = await DiscoveryOrchestrator._attempt_browser_provider(
        provider=provider,
        criteria=criteria,
        config=config,
        settings=settings,
        session_store=session_store,
        capture=_make_capture(),
        storage_state=None,
    )

    assert isinstance(result, ProviderResult)
    assert any("auth_missing" in e for e in result.errors)


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
    session_store.invalidate = MagicMock()
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

    result = await DiscoveryOrchestrator._attempt_browser_provider(
        provider=provider,
        criteria=criteria,
        config=config,
        settings=settings,
        session_store=session_store,
        capture=_make_capture(),
        storage_state=None,
    )

    assert isinstance(result, ProviderResult)
    assert events.index("wait") < events.index("goto")


@pytest.mark.asyncio
async def test_auth_failure_with_session_triggers_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When auth fails and a session existed, the orchestrator should
    invalidate the session and retry once."""
    attempt_count = 0

    class _FailThenSucceedProvider(BaseProvider):
        source = JobSource.LINKEDIN

        def requires_auth(self) -> bool:
            return True

        def base_domain(self) -> str:
            return "www.linkedin.com"

        async def is_authenticated(self, page: object) -> bool:
            return False

        async def authenticate(
            self, page: object, *, email: str, password: str, behavior: object
        ) -> bool:
            nonlocal attempt_count
            attempt_count += 1
            return attempt_count >= 2

        def _build_search_urls(
            self, criteria: SearchCriteria, config: DiscoveryConfig
        ) -> list[str]:
            return []

        async def _extract_refs_from_page(self, page: object) -> list[ListingRef]:
            return []

    provider = _FailThenSucceedProvider()
    criteria = SearchCriteria()
    config = DiscoveryConfig(sessions_dir="/tmp/sessions")
    settings = MagicMock(
        linkedin_email="user@example.com",
        linkedin_password=SecretStr("secret"),
        wellfound_email="",
        wellfound_password=SecretStr(""),
    )
    session_store = MagicMock()
    session_store.load.return_value = {"cookies": []}
    session_store.save = AsyncMock(return_value=True)
    session_store.age_hours.return_value = 1.0
    session_store.invalidate = MagicMock()
    semaphore = asyncio.Semaphore(1)

    fake_page = AsyncMock()
    fake_page.goto = AsyncMock()
    fake_page.url = "https://www.linkedin.com/login"
    fake_page.query_selector = AsyncMock(return_value=None)

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
        _make_capture(),
    )

    assert isinstance(result, ProviderResult)
    session_store.invalidate.assert_called_once_with(JobSource.LINKEDIN)
    assert attempt_count == 2


@pytest.mark.asyncio
async def test_auth_failure_does_not_save_poisoned_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: 2026-04-14 Run 2 saved a half-complete login state as
    the session file, and Run 3 loaded it and failed auth again. The
    finally block must skip ``session_store.save`` when auth failed and
    invalidate any loaded state instead."""

    class _AlwaysFailProvider(BaseProvider):
        source = JobSource.LINKEDIN

        def requires_auth(self) -> bool:
            return True

        def base_domain(self) -> str:
            return "www.linkedin.com"

        async def is_authenticated(self, page: object) -> bool:
            return False

        async def authenticate(
            self, page: object, *, email: str, password: str, behavior: object
        ) -> bool:
            return False

        def _build_search_urls(
            self, criteria: SearchCriteria, config: DiscoveryConfig
        ) -> list[str]:
            return []

        async def _extract_refs_from_page(self, page: object) -> list[ListingRef]:
            return []

    provider = _AlwaysFailProvider()
    criteria = SearchCriteria()
    config = DiscoveryConfig(sessions_dir="/tmp/sessions")
    settings = MagicMock(
        linkedin_email="user@example.com",
        linkedin_password=SecretStr("secret"),
        wellfound_email="",
        wellfound_password=SecretStr(""),
    )
    session_store = MagicMock()
    session_store.load.return_value = {"cookies": []}
    session_store.save = AsyncMock(return_value=True)
    session_store.invalidate = MagicMock()
    session_store.age_hours.return_value = 1.0

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

    result = await DiscoveryOrchestrator._attempt_browser_provider(
        provider=provider,
        criteria=criteria,
        config=config,
        settings=settings,
        session_store=session_store,
        capture=_make_capture(),
        storage_state={"cookies": [{"fake": "cookie"}]},
    )

    assert isinstance(result, ProviderResult)
    assert any("auth_failed" in e for e in result.errors)
    # The critical assertion: save must NOT have been called when auth failed.
    session_store.save.assert_not_awaited()
    # And the poisoned session file on disk must be cleaned up.
    session_store.invalidate.assert_called_with(JobSource.LINKEDIN)


@pytest.mark.asyncio
async def test_auth_success_still_saves_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive case: successful auth should still persist the session."""

    class _OkProvider(BaseProvider):
        source = JobSource.LINKEDIN

        def requires_auth(self) -> bool:
            return True

        def base_domain(self) -> str:
            return "www.linkedin.com"

        async def is_authenticated(self, page: object) -> bool:
            return False

        async def authenticate(
            self, page: object, *, email: str, password: str, behavior: object
        ) -> bool:
            return True

        async def run_search(
            self,
            page: object,
            criteria: SearchCriteria,
            config: DiscoveryConfig,
            *,
            behavior: object,
            rate_limiter: object,
            captcha_handler: object,
            capture: object,
        ) -> list[ListingRef]:
            return []

        def _build_search_urls(
            self, criteria: SearchCriteria, config: DiscoveryConfig
        ) -> list[str]:
            return []

        async def _extract_refs_from_page(self, page: object) -> list[ListingRef]:
            return []

    provider = _OkProvider()
    criteria = SearchCriteria()
    config = DiscoveryConfig(sessions_dir="/tmp/sessions")
    settings = MagicMock(
        linkedin_email="user@example.com",
        linkedin_password=SecretStr("secret"),
    )
    session_store = MagicMock()
    session_store.load.return_value = None
    session_store.save = AsyncMock(return_value=True)
    session_store.invalidate = MagicMock()
    session_store.age_hours.return_value = None

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

    result = await DiscoveryOrchestrator._attempt_browser_provider(
        provider=provider,
        criteria=criteria,
        config=config,
        settings=settings,
        session_store=session_store,
        capture=_make_capture(),
        storage_state=None,
    )

    assert isinstance(result, ProviderResult)
    assert result.errors == []
    # Successful auth path: save must run, invalidate must not.
    session_store.save.assert_awaited_once()
    session_store.invalidate.assert_not_called()
