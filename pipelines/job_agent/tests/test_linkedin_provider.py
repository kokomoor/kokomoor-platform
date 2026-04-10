"""Tests for the LinkedIn provider — URL builder unit tests only.

Full browser tests require a real LinkedIn session and are marked
@pytest.mark.integration (not run here).
"""

from __future__ import annotations

from pipelines.job_agent.discovery.models import DiscoveryConfig
from pipelines.job_agent.discovery.providers.linkedin import LinkedInProvider
from pipelines.job_agent.models import SearchCriteria

_DEFAULT_CONFIG = DiscoveryConfig(sessions_dir="/tmp/test-sessions")


class TestBuildSearchUrls:
    def test_keywords_with_location(self) -> None:
        provider = LinkedInProvider()
        criteria = SearchCriteria(
            keywords=["python", "engineer"],
            locations=["San Francisco, CA"],
        )
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert len(urls) == 1
        assert "keywords=python+engineer" in urls[0]
        assert "San+Francisco" in urls[0]

    def test_target_roles_preferred_over_keywords(self) -> None:
        provider = LinkedInProvider()
        criteria = SearchCriteria(
            keywords=["python"],
            target_roles=["Software Engineer", "Backend Developer"],
            locations=["New York"],
        )
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert len(urls) == 2
        assert "Software+Engineer" in urls[0]
        assert "Backend+Developer" in urls[1]
        assert not any("python" in u for u in urls)

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

    def test_keywords_truncated_to_three(self) -> None:
        provider = LinkedInProvider()
        criteria = SearchCriteria(keywords=["alpha", "beta", "gamma", "delta", "epsilon"])
        urls = provider._build_search_urls(criteria, _DEFAULT_CONFIG)

        assert "alpha+beta+gamma" in urls[0]
        assert "delta" not in urls[0]

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
