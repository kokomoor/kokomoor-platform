"""Tests for BaseProvider using a concrete test subclass with mocked Playwright."""

from __future__ import annotations

from typing import ClassVar
from unittest.mock import AsyncMock

import pytest

from pipelines.job_agent.discovery.captcha import CaptchaDetection, CaptchaOutcome
from pipelines.job_agent.discovery.models import DiscoveryConfig, ListingRef
from pipelines.job_agent.discovery.providers.base import BaseProvider
from pipelines.job_agent.models import JobSource, SearchCriteria

_CONFIG = DiscoveryConfig(
    sessions_dir="/tmp/test",
    max_pages_per_search=3,
    max_listings_per_provider=10,
)


class _TestProvider(BaseProvider):
    source: ClassVar[JobSource] = JobSource.OTHER

    def base_domain(self) -> str:
        return "example.com"

    async def is_authenticated(self, page: object) -> bool:
        return True

    def _build_search_urls(self, criteria: SearchCriteria, config: DiscoveryConfig) -> list[str]:
        return ["https://example.com/jobs"]

    async def _extract_refs_from_page(self, page: object) -> list[ListingRef]:
        return [
            ListingRef(
                url="https://example.com/job/1",
                title="Engineer",
                company="TestCo",
                source=JobSource.OTHER,
            )
        ]

    def _next_page_selector(self) -> str | None:
        return "button.next"


def _make_page(*, has_next: bool = False) -> AsyncMock:
    page = AsyncMock()
    page.goto = AsyncMock()

    if has_next:
        next_btn = AsyncMock()
        next_btn.is_visible = AsyncMock(return_value=True)
        page.query_selector = AsyncMock(return_value=next_btn)
    else:
        page.query_selector = AsyncMock(return_value=None)

    return page


def _make_captcha_handler(*, detected: bool = False) -> AsyncMock:
    handler = AsyncMock()
    handler.detect = AsyncMock(return_value=CaptchaDetection(detected=detected))
    handler.handle = AsyncMock(return_value=CaptchaOutcome(resolved=False, strategy_used="skipped"))
    return handler


def _make_rate_limiter() -> AsyncMock:
    rl = AsyncMock()
    rl.wait = AsyncMock()
    rl.page_count = 0
    return rl


def _make_behavior() -> AsyncMock:
    b = AsyncMock()
    b.between_actions_pause = AsyncMock()
    b.simulate_interest_in_page = AsyncMock()
    b.human_click = AsyncMock()
    return b


class TestRunSingleSearch:
    @pytest.mark.asyncio
    async def test_successful_extraction(self) -> None:
        provider = _TestProvider()
        page = _make_page()
        refs = await provider._run_single_search(
            page,
            "https://example.com/jobs",
            _CONFIG,
            behavior=_make_behavior(),
            rate_limiter=_make_rate_limiter(),
            captcha_handler=_make_captcha_handler(),
        )
        assert len(refs) == 1
        assert refs[0].title == "Engineer"

    @pytest.mark.asyncio
    async def test_captcha_detected_returns_empty(self) -> None:
        provider = _TestProvider()
        page = _make_page()
        handler = _make_captcha_handler(detected=True)

        refs = await provider._run_single_search(
            page,
            "https://example.com/jobs",
            _CONFIG,
            behavior=_make_behavior(),
            rate_limiter=_make_rate_limiter(),
            captcha_handler=handler,
        )
        assert refs == []
        handler.handle.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_navigation_timeout_returns_empty(self) -> None:
        provider = _TestProvider()
        page = AsyncMock()
        page.goto = AsyncMock(side_effect=TimeoutError("nav timeout"))

        refs = await provider._run_single_search(
            page,
            "https://example.com/jobs",
            _CONFIG,
            behavior=_make_behavior(),
            rate_limiter=_make_rate_limiter(),
            captcha_handler=_make_captcha_handler(),
        )
        assert refs == []

    @pytest.mark.asyncio
    async def test_wait_happens_before_initial_navigation(self) -> None:
        provider = _TestProvider()
        page = AsyncMock()
        events: list[str] = []

        async def _wait() -> None:
            events.append("wait")

        async def _goto(*args: object, **kwargs: object) -> None:
            events.append("goto")

        page.goto = AsyncMock(side_effect=_goto)
        page.query_selector = AsyncMock(return_value=None)

        rate_limiter = _make_rate_limiter()
        rate_limiter.wait = AsyncMock(side_effect=_wait)

        await provider._run_single_search(
            page,
            "https://example.com/jobs",
            _CONFIG,
            behavior=_make_behavior(),
            rate_limiter=rate_limiter,
            captcha_handler=_make_captcha_handler(),
        )

        assert events.index("wait") < events.index("goto")


class TestPagination:
    @pytest.mark.asyncio
    async def test_next_button_found_paginates(self) -> None:
        provider = _TestProvider()
        page = _make_page(has_next=True)

        refs = await provider._run_single_search(
            page,
            "https://example.com/jobs",
            _CONFIG,
            behavior=_make_behavior(),
            rate_limiter=_make_rate_limiter(),
            captcha_handler=_make_captcha_handler(),
        )
        assert len(refs) >= 2

    @pytest.mark.asyncio
    async def test_no_next_button_stops(self) -> None:
        provider = _TestProvider()
        page = _make_page(has_next=False)

        refs = await provider._run_single_search(
            page,
            "https://example.com/jobs",
            _CONFIG,
            behavior=_make_behavior(),
            rate_limiter=_make_rate_limiter(),
            captcha_handler=_make_captcha_handler(),
        )
        assert len(refs) == 1

    @pytest.mark.asyncio
    async def test_max_pages_respected(self) -> None:
        config = DiscoveryConfig(
            sessions_dir="/tmp/test",
            max_pages_per_search=2,
            max_listings_per_provider=100,
        )
        provider = _TestProvider()
        page = _make_page(has_next=True)

        refs = await provider._run_single_search(
            page,
            "https://example.com/jobs",
            config,
            behavior=_make_behavior(),
            rate_limiter=_make_rate_limiter(),
            captcha_handler=_make_captcha_handler(),
        )
        assert len(refs) <= 2

    @pytest.mark.asyncio
    async def test_max_listings_cap(self) -> None:
        config = DiscoveryConfig(
            sessions_dir="/tmp/test",
            max_pages_per_search=20,
            max_listings_per_provider=2,
        )

        class _MultiRefProvider(_TestProvider):
            async def _extract_refs_from_page(self, page: object) -> list[ListingRef]:
                return [
                    ListingRef(
                        url=f"https://example.com/job/{i}",
                        title=f"Engineer {i}",
                        company="TestCo",
                        source=JobSource.OTHER,
                    )
                    for i in range(5)
                ]

        provider = _MultiRefProvider()
        page = _make_page(has_next=True)

        refs = await provider.run_search(
            page,
            SearchCriteria(),
            config,
            behavior=_make_behavior(),
            rate_limiter=_make_rate_limiter(),
            captcha_handler=_make_captcha_handler(),
        )
        assert len(refs) <= 2

    @pytest.mark.asyncio
    async def test_wait_happens_before_next_click(self) -> None:
        config = DiscoveryConfig(
            sessions_dir="/tmp/test",
            max_pages_per_search=2,
            max_listings_per_provider=100,
        )
        provider = _TestProvider()
        page = _make_page(has_next=True)
        events: list[str] = []

        async def _wait() -> None:
            events.append("wait")

        async def _click(*args: object, **kwargs: object) -> None:
            events.append("click")

        rate_limiter = _make_rate_limiter()
        rate_limiter.wait = AsyncMock(side_effect=_wait)
        behavior = _make_behavior()
        behavior.human_click = AsyncMock(side_effect=_click)

        await provider._run_single_search(
            page,
            "https://example.com/jobs",
            config,
            behavior=behavior,
            rate_limiter=rate_limiter,
            captcha_handler=_make_captcha_handler(),
        )

        # First wait is for initial navigation; second wait precedes pagination click.
        assert events.count("wait") >= 2
        assert events.count("click") >= 1
        assert events.index("wait", 1) < events.index("click")


class TestRunSearch:
    @pytest.mark.asyncio
    async def test_aggregates_from_multiple_urls(self) -> None:
        class _MultiUrlProvider(_TestProvider):
            def _build_search_urls(
                self, criteria: SearchCriteria, config: DiscoveryConfig
            ) -> list[str]:
                return ["https://example.com/jobs/1", "https://example.com/jobs/2"]

        provider = _MultiUrlProvider()
        page = _make_page()

        refs = await provider.run_search(
            page,
            SearchCriteria(),
            _CONFIG,
            behavior=_make_behavior(),
            rate_limiter=_make_rate_limiter(),
            captcha_handler=_make_captcha_handler(),
        )
        assert len(refs) == 2
