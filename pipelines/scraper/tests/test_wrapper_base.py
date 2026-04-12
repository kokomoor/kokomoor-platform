"""Tests for BaseSiteWrapper — extraction, URL normalization, fixture-based scraping.

These tests run against offline HTML fixtures (no browser, no network).
They validate that the extraction logic correctly parses records from
the HTML and that the contract is satisfied.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from pipelines.scraper.tests.conftest import (
    SAMPLE_GOLDEN_RECORDS,
    SAMPLE_LISTING_HTML,
    SAMPLE_LISTING_HTML_DRIFTED,
)
from pipelines.scraper.wrappers.base import BaseSiteWrapper

if TYPE_CHECKING:
    from pipelines.scraper.models import SiteProfile


class TestExtractFromFixture:
    """Offline fixture extraction tests."""

    def _make_wrapper(self, profile: SiteProfile) -> BaseSiteWrapper:
        wrapper = BaseSiteWrapper.__new__(BaseSiteWrapper)
        wrapper._profile = profile
        wrapper._errors = []
        wrapper._warnings = []
        return wrapper

    def test_extracts_all_records(self, sample_profile: SiteProfile) -> None:
        wrapper = self._make_wrapper(sample_profile)
        records = wrapper.extract_from_fixture(SAMPLE_LISTING_HTML)
        assert len(records) == 3

    def test_extracts_correct_fields(self, sample_profile: SiteProfile) -> None:
        wrapper = self._make_wrapper(sample_profile)
        records = wrapper.extract_from_fixture(SAMPLE_LISTING_HTML)
        assert records[0]["title"] == "Widget A"
        assert records[0]["description"] == "A fine widget"
        assert records[0]["price"] == "$10.00"

    def test_normalizes_relative_urls(self, sample_profile: SiteProfile) -> None:
        wrapper = self._make_wrapper(sample_profile)
        records = wrapper.extract_from_fixture(SAMPLE_LISTING_HTML)
        assert records[0]["url"] == "https://test.example.com/items/1"
        assert records[1]["url"] == "https://test.example.com/items/2"

    def test_matches_golden_records(self, sample_profile: SiteProfile) -> None:
        wrapper = self._make_wrapper(sample_profile)
        records = wrapper.extract_from_fixture(SAMPLE_LISTING_HTML)
        for extracted, golden in zip(records, SAMPLE_GOLDEN_RECORDS, strict=True):
            for key in golden:
                assert extracted.get(key) == golden[key], (
                    f"Field '{key}': expected {golden[key]!r}, got {extracted.get(key)!r}"
                )

    def test_empty_html_returns_empty(self, sample_profile: SiteProfile) -> None:
        wrapper = self._make_wrapper(sample_profile)
        records = wrapper.extract_from_fixture("<html><body></body></html>")
        assert records == []

    def test_partial_records_included(self, sample_profile: SiteProfile) -> None:
        html = """
        <html><body>
        <div class="listing-card">
            <h3 class="title">Only Title</h3>
        </div>
        </body></html>
        """
        wrapper = self._make_wrapper(sample_profile)
        records = wrapper.extract_from_fixture(html)
        assert len(records) == 1
        assert records[0]["title"] == "Only Title"
        assert records[0]["url"] == ""

    def test_drifted_html_extracts_nothing(self, sample_profile: SiteProfile) -> None:
        """Drifted HTML with changed class names should fail extraction."""
        wrapper = self._make_wrapper(sample_profile)
        records = wrapper.extract_from_fixture(SAMPLE_LISTING_HTML_DRIFTED)
        assert len(records) <= 1


class TestUrlNormalization:
    def _make_wrapper(self, profile: SiteProfile) -> BaseSiteWrapper:
        wrapper = BaseSiteWrapper.__new__(BaseSiteWrapper)
        wrapper._profile = profile
        wrapper._errors = []
        wrapper._warnings = []
        return wrapper

    def test_relative_path(self, sample_profile: SiteProfile) -> None:
        wrapper = self._make_wrapper(sample_profile)
        assert wrapper._normalize_url("/foo/bar") == "https://test.example.com/foo/bar"

    def test_protocol_relative(self, sample_profile: SiteProfile) -> None:
        wrapper = self._make_wrapper(sample_profile)
        assert wrapper._normalize_url("//cdn.example.com/x") == "https://cdn.example.com/x"

    def test_absolute_unchanged(self, sample_profile: SiteProfile) -> None:
        wrapper = self._make_wrapper(sample_profile)
        url = "https://other.com/page"
        assert wrapper._normalize_url(url) == url


class TestSearchUrlBuild:
    def _make_wrapper(self, profile: SiteProfile) -> BaseSiteWrapper:
        wrapper = BaseSiteWrapper.__new__(BaseSiteWrapper)
        wrapper._profile = profile
        wrapper._errors = []
        wrapper._warnings = []
        return wrapper

    def test_builds_with_params(self, sample_profile: SiteProfile) -> None:
        wrapper = self._make_wrapper(sample_profile)
        url = wrapper._build_search_url({"query": "widgets"}, page=2)
        assert url == "https://test.example.com/search?q=widgets&page=2"

    def test_handles_missing_params_gracefully(self, sample_profile: SiteProfile) -> None:
        """When search params are missing, template is returned as-is (graceful fallback)."""
        wrapper = self._make_wrapper(sample_profile)
        url = wrapper._build_search_url({}, page=1)
        assert "test.example.com" in url

    @pytest.mark.asyncio
    async def test_paginate_stops_on_unresolved_template(self, sample_profile: SiteProfile) -> None:
        wrapper = self._make_wrapper(sample_profile)
        wrapper._actions = AsyncMock()
        wrapper._profile.navigation.search_url_template = (
            "https://x.test/search?q={query}&page={page}"
        )
        has_next = await wrapper._do_paginate(current_page=1)
        assert not has_next
