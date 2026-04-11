"""Tests for the LinkedIn provider -- URL builder, auth detection, and mode handling.

Full browser tests require a real LinkedIn session and are marked
@pytest.mark.integration (not run here).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pipelines.job_agent.discovery.models import DiscoveryConfig
from pipelines.job_agent.discovery.providers.linkedin import LinkedInProvider
from pipelines.job_agent.models import SearchCriteria

_DEFAULT_CONFIG = DiscoveryConfig(sessions_dir="/tmp/test-sessions")


class TestBuildSearchUrls:
    def test_keywords_each_get_own_url(self) -> None:
        provider = LinkedInProvider()
        criteria = SearchCriteria(
            keywords=["python", "engineer"],
            locations=["San Francisco, CA"],
        )
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert len(urls) == 2
        assert "keywords=python" in urls[0]
        assert "keywords=engineer" in urls[1]
        assert all("San+Francisco" in u for u in urls)

    def test_target_roles_and_keywords_both_generate_urls(self) -> None:
        provider = LinkedInProvider()
        criteria = SearchCriteria(
            keywords=["python"],
            target_roles=["Software Engineer", "Backend Developer"],
            locations=["New York"],
        )
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert len(urls) == 3
        assert "Software+Engineer" in urls[0]
        assert "Backend+Developer" in urls[1]
        assert "python" in urls[2]

    def test_remote_filter_added_when_remote_ok(self) -> None:
        provider = LinkedInProvider()
        criteria = SearchCriteria(keywords=["engineer"], remote_ok=True)
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert "f_WT=2" in urls[0]

    def test_no_remote_filter_when_not_remote(self) -> None:
        provider = LinkedInProvider()
        criteria = SearchCriteria(keywords=["engineer"], remote_ok=False, locations=["Boston"])
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert "f_WT" not in urls[0]

    def test_default_location_is_united_states(self) -> None:
        provider = LinkedInProvider()
        criteria = SearchCriteria(keywords=["engineer"])
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert "United+States" in urls[0]

    def test_sort_by_date_descending(self) -> None:
        provider = LinkedInProvider()
        criteria = SearchCriteria(keywords=["test"])
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert "sortBy=DD" in urls[0]

    def test_past_week_filter_present(self) -> None:
        provider = LinkedInProvider()
        criteria = SearchCriteria(keywords=["test"])
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert "f_TPR=r604800" in urls[0]

    def test_max_six_urls(self) -> None:
        provider = LinkedInProvider()
        criteria = SearchCriteria(
            target_roles=["SWE", "PM", "Designer", "Analyst"],
            locations=["NYC", "SF", "Seattle"],
        )
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert len(urls) <= 6

    def test_each_keyword_is_separate_url(self) -> None:
        provider = LinkedInProvider()
        criteria = SearchCriteria(keywords=["alpha", "beta", "gamma", "delta", "epsilon"])
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert len(urls) == 5
        assert "alpha" in urls[0]
        assert "beta" in urls[1]

    def test_multiple_locations_generate_urls(self) -> None:
        provider = LinkedInProvider()
        criteria = SearchCriteria(
            keywords=["engineer"],
            locations=["New York", "San Francisco"],
        )
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert len(urls) == 2
        assert "New+York" in urls[0]
        assert "San+Francisco" in urls[1]

    def test_empty_criteria_uses_fallback(self) -> None:
        provider = LinkedInProvider()
        criteria = SearchCriteria()
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert len(urls) >= 1
        assert "software+engineer" in urls[0]

    def test_locations_capped_at_three(self) -> None:
        provider = LinkedInProvider()
        criteria = SearchCriteria(
            keywords=["engineer"],
            locations=["NYC", "SF", "Seattle", "Austin", "Chicago"],
        )
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert len(urls) == 3

    def test_role_words_capped_at_three(self) -> None:
        provider = LinkedInProvider()
        criteria = SearchCriteria(target_roles=["Senior Staff Software Engineer"])
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert "Senior+Staff+Software" in urls[0]
        assert "Engineer" not in urls[0]


class TestProviderAttributes:
    def test_requires_auth(self) -> None:
        assert LinkedInProvider().requires_auth() is True

    def test_base_domain(self) -> None:
        assert LinkedInProvider().base_domain() == "www.linkedin.com"

    def test_source(self) -> None:
        from pipelines.job_agent.models import JobSource

        assert LinkedInProvider.source == JobSource.LINKEDIN

    def test_next_page_selector(self) -> None:
        sel = LinkedInProvider()._next_page_selector()
        assert sel is not None
        assert "View next page" in sel


class TestAuthDetection:
    """Tests for is_authenticated() across different page states."""

    @pytest.mark.asyncio
    async def test_authenticated_on_feed_url(self) -> None:
        provider = LinkedInProvider()
        page = AsyncMock()
        page.url = "https://www.linkedin.com/feed/"
        page.query_selector = AsyncMock(return_value=None)
        page.evaluate = AsyncMock(return_value=False)

        assert await provider.is_authenticated(page) is True

    @pytest.mark.asyncio
    async def test_jobs_url_alone_not_authenticated(self) -> None:
        """The ``/jobs/`` URL family is served to guests too (e.g. public
        job-view pages), so the URL alone must not be a positive signal.
        """
        provider = LinkedInProvider()
        page = AsyncMock()
        page.url = "https://www.linkedin.com/jobs/search/?keywords=engineer"
        page.query_selector = AsyncMock(return_value=None)
        page.evaluate = AsyncMock(return_value=False)

        assert await provider.is_authenticated(page) is False

    @pytest.mark.asyncio
    async def test_authenticated_on_mynetwork_url(self) -> None:
        provider = LinkedInProvider()
        page = AsyncMock()
        page.url = "https://www.linkedin.com/mynetwork/"
        page.query_selector = AsyncMock(return_value=None)
        page.evaluate = AsyncMock(return_value=False)

        assert await provider.is_authenticated(page) is True

    @pytest.mark.asyncio
    async def test_not_authenticated_on_login_url(self) -> None:
        provider = LinkedInProvider()
        page = AsyncMock()
        page.url = "https://www.linkedin.com/login?fromSignIn=true"

        assert await provider.is_authenticated(page) is False

    @pytest.mark.asyncio
    async def test_not_authenticated_on_checkpoint(self) -> None:
        provider = LinkedInProvider()
        page = AsyncMock()
        page.url = "https://www.linkedin.com/checkpoint/challenge/123"

        assert await provider.is_authenticated(page) is False

    @pytest.mark.asyncio
    async def test_not_authenticated_when_login_fields_present(self) -> None:
        provider = LinkedInProvider()
        page = AsyncMock()
        page.url = "https://www.linkedin.com/"
        page.query_selector = AsyncMock(return_value=object())

        assert await provider.is_authenticated(page) is False

    @pytest.mark.asyncio
    async def test_not_authenticated_on_guest_jobs(self) -> None:
        provider = LinkedInProvider()
        page = AsyncMock()
        page.url = "https://www.linkedin.com/jobs/guest/search"
        page.query_selector = AsyncMock(return_value=None)
        page.evaluate = AsyncMock(return_value=False)

        assert await provider.is_authenticated(page) is False

    @pytest.mark.asyncio
    async def test_authenticated_via_global_nav_selector(self) -> None:
        provider = LinkedInProvider()
        page = AsyncMock()
        page.url = "https://www.linkedin.com/"

        async def _query_selector(sel: str) -> object | None:
            if sel in {"#global-nav", ".global-nav__primary-items"}:
                return object()
            return None

        page.query_selector = _query_selector
        page.evaluate = AsyncMock(return_value=False)

        assert await provider.is_authenticated(page) is True


class TestSearchPageVerification:
    """Test that _verify_search_page detects redirects to login/authwall."""

    @pytest.mark.asyncio
    async def test_rejects_login_redirect(self) -> None:
        provider = LinkedInProvider()
        page = AsyncMock()
        page.url = "https://www.linkedin.com/login?trk=guest"

        assert await provider._verify_search_page(page, "https://expected.url") is False

    @pytest.mark.asyncio
    async def test_rejects_authwall(self) -> None:
        provider = LinkedInProvider()
        page = AsyncMock()
        page.url = "https://www.linkedin.com/authwall?trk=guest"

        assert await provider._verify_search_page(page, "https://expected.url") is False

    @pytest.mark.asyncio
    async def test_accepts_valid_search_page(self) -> None:
        provider = LinkedInProvider()
        page = AsyncMock()
        page.url = "https://www.linkedin.com/jobs/search/?keywords=engineer"

        assert await provider._verify_search_page(page, "https://expected.url") is True
