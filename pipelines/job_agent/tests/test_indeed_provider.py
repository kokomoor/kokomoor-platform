"""Tests for the Indeed browser provider.

Uses AsyncMock for Playwright Page objects — no real browser needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pipelines.job_agent.discovery.models import DiscoveryConfig
from pipelines.job_agent.discovery.providers.indeed import IndeedProvider
from pipelines.job_agent.models import JobSource, SearchCriteria

_DEFAULT_CONFIG = DiscoveryConfig(sessions_dir="/tmp/test-sessions")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_card(
    jk: str | None,
    title: str,
    company: str,
    location: str,
    salary: str | None = None,
) -> AsyncMock:
    """Build a mock DOM element mimicking an Indeed job card."""
    card = AsyncMock()

    jk_link: AsyncMock | None = None
    if jk is not None:
        jk_link = AsyncMock()
        jk_link.get_attribute = AsyncMock(return_value=jk)

    title_el = AsyncMock()
    title_el.text_content = AsyncMock(return_value=title)

    company_el = AsyncMock()
    company_el.text_content = AsyncMock(return_value=company)

    location_el = AsyncMock()
    location_el.text_content = AsyncMock(return_value=location)

    salary_el: AsyncMock | None = None
    if salary is not None:
        salary_el = AsyncMock()
        salary_el.text_content = AsyncMock(return_value=salary)

    async def mock_qs(selector: str) -> AsyncMock | None:
        if "data-jk" in selector:
            return jk_link
        if "jobTitle" in selector:
            return title_el
        if "company-name" in selector:
            return company_el
        if "text-location" in selector:
            return location_el
        if "attribute_snippet" in selector or "salary" in selector:
            return salary_el
        return None

    card.query_selector = mock_qs
    card.get_attribute = AsyncMock(return_value=jk)

    return card


# ---------------------------------------------------------------------------
# _build_search_urls
# ---------------------------------------------------------------------------


class TestBuildSearchUrls:
    def test_keywords_only(self) -> None:
        provider = IndeedProvider()
        criteria = SearchCriteria(keywords=["python", "engineer"])
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert len(urls) == 1
        assert "q=python+engineer" in urls[0]
        assert "fromage=14" in urls[0]

    def test_with_explicit_location(self) -> None:
        provider = IndeedProvider()
        criteria = SearchCriteria(keywords=["engineer"], locations=["San Francisco, CA"])
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert len(urls) == 1
        assert "San+Francisco" in urls[0]

    def test_remote_default_when_no_locations(self) -> None:
        provider = IndeedProvider()
        criteria = SearchCriteria(keywords=["engineer"], remote_ok=True)
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert "l=Remote" in urls[0]

    def test_empty_location_when_not_remote(self) -> None:
        provider = IndeedProvider()
        criteria = SearchCriteria(keywords=["engineer"], remote_ok=False)
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert "l=&" in urls[0] or "l=0" not in urls[0]

    def test_target_roles_add_extra_urls(self) -> None:
        provider = IndeedProvider()
        criteria = SearchCriteria(
            keywords=["python"],
            target_roles=["Software Engineer", "Backend Developer"],
        )
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert len(urls) == 3
        assert "q=python" in urls[0]
        assert "Software+Engineer" in urls[1]
        assert "Backend+Developer" in urls[2]

    def test_capped_at_three_urls(self) -> None:
        provider = IndeedProvider()
        criteria = SearchCriteria(
            keywords=["python"],
            target_roles=["SWE", "PM", "Designer", "Analyst"],
        )
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert len(urls) == 3

    def test_empty_criteria_still_returns_one_url(self) -> None:
        provider = IndeedProvider()
        criteria = SearchCriteria(remote_ok=True)
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert len(urls) == 1
        assert "l=Remote" in urls[0]

    def test_keywords_truncated_to_four(self) -> None:
        provider = IndeedProvider()
        criteria = SearchCriteria(keywords=["alpha", "beta", "gamma", "delta", "epsilon", "zeta"])
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert "q=alpha+beta+gamma+delta" in urls[0]
        assert "epsilon" not in urls[0]

    def test_fulltime_filter_present(self) -> None:
        provider = IndeedProvider()
        criteria = SearchCriteria(keywords=["test"])
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert "DSQF7" in urls[0]


# ---------------------------------------------------------------------------
# _extract_refs_from_page
# ---------------------------------------------------------------------------


class TestExtractRefsFromPage:
    @pytest.mark.asyncio
    async def test_extracts_refs_from_cards(self) -> None:
        card = _make_mock_card(
            "abc123", "Software Engineer", "Acme Corp", "Remote", "$180K - $200K"
        )
        page = AsyncMock()
        page.query_selector_all = AsyncMock(return_value=[card])

        provider = IndeedProvider()
        refs = await provider._extract_refs_from_page(page)

        assert len(refs) == 1
        assert refs[0].title == "Software Engineer"
        assert refs[0].company == "Acme Corp"
        assert refs[0].location == "Remote"
        assert refs[0].source == JobSource.INDEED
        assert "jk=abc123" in refs[0].url
        assert refs[0].salary_text == "$180K - $200K"

    @pytest.mark.asyncio
    async def test_missing_jk_skips_card(self) -> None:
        card = _make_mock_card(None, "Engineer", "Corp", "NYC")
        page = AsyncMock()
        page.query_selector_all = AsyncMock(return_value=[card])

        provider = IndeedProvider()
        refs = await provider._extract_refs_from_page(page)

        assert refs == []

    @pytest.mark.asyncio
    async def test_duplicate_jk_deduped(self) -> None:
        card1 = _make_mock_card("same_jk", "Engineer A", "Corp A", "NYC")
        card2 = _make_mock_card("same_jk", "Engineer B", "Corp B", "SF")
        page = AsyncMock()
        page.query_selector_all = AsyncMock(return_value=[card1, card2])

        provider = IndeedProvider()
        refs = await provider._extract_refs_from_page(page)

        assert len(refs) == 1
        assert refs[0].title == "Engineer A"

    @pytest.mark.asyncio
    async def test_fallback_to_beacon_selector(self) -> None:
        card = _make_mock_card("xyz789", "Dev", "Co", "LA")
        page = AsyncMock()

        call_count = 0

        async def mock_qsa(selector: str) -> list[AsyncMock]:
            nonlocal call_count
            call_count += 1
            if "slider_item" in selector:
                return []
            return [card]

        page.query_selector_all = mock_qsa

        provider = IndeedProvider()
        refs = await provider._extract_refs_from_page(page)

        assert len(refs) == 1
        assert refs[0].title == "Dev"

    @pytest.mark.asyncio
    async def test_missing_salary_returns_empty_string(self) -> None:
        card = _make_mock_card("jk1", "Engineer", "Corp", "NYC", salary=None)
        page = AsyncMock()
        page.query_selector_all = AsyncMock(return_value=[card])

        provider = IndeedProvider()
        refs = await provider._extract_refs_from_page(page)

        assert len(refs) == 1
        assert refs[0].salary_text == ""

    @pytest.mark.asyncio
    async def test_url_is_canonicalized(self) -> None:
        card = _make_mock_card("canon_test", "Role", "Co", "Remote")
        page = AsyncMock()
        page.query_selector_all = AsyncMock(return_value=[card])

        provider = IndeedProvider()
        refs = await provider._extract_refs_from_page(page)

        assert len(refs) == 1
        assert refs[0].url == "https://www.indeed.com/viewjob?jk=canon_test"

    @pytest.mark.asyncio
    async def test_multiple_cards_extracted(self) -> None:
        cards = [_make_mock_card(f"jk{i}", f"Role {i}", f"Co {i}", "NYC") for i in range(5)]
        page = AsyncMock()
        page.query_selector_all = AsyncMock(return_value=cards)

        provider = IndeedProvider()
        refs = await provider._extract_refs_from_page(page)

        assert len(refs) == 5

    @pytest.mark.asyncio
    async def test_empty_page_returns_empty(self) -> None:
        page = AsyncMock()
        page.query_selector_all = AsyncMock(return_value=[])

        provider = IndeedProvider()
        refs = await provider._extract_refs_from_page(page)

        assert refs == []
