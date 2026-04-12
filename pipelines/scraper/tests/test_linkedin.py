"""Offline fixture tests for LinkedInWrapper."""

from __future__ import annotations

from unittest.mock import MagicMock

from pipelines.scraper.models import (
    AuthConfig,
    AuthType,
    FieldSpec,
    NavigationConfig,
    OutputContract,
    PaginationStrategy,
    RateLimitConfig,
    SelectorConfig,
    SiteProfile,
)
from pipelines.scraper.wrappers.linkedin import LinkedInWrapper

LINKEDIN_JOB_CARDS_HTML = """
<html><body>
<ul class="jobs-search__results-list">
  <li>
    <div class="base-card job-search-card">
      <h3 class="base-search-card__title">Senior Software Engineer</h3>
      <h4 class="base-search-card__subtitle">
        <a href="https://www.linkedin.com/company/acme-corp">Acme Corp</a>
      </h4>
      <span class="job-search-card__location">San Francisco, CA</span>
      <a href="https://www.linkedin.com/jobs/view/12345?trk=abc">View</a>
    </div>
  </li>
  <li>
    <div class="base-card job-search-card">
      <h3 class="base-search-card__title">Backend Developer</h3>
      <h4 class="base-search-card__subtitle">
        <a href="https://www.linkedin.com/company/beta-inc">Beta Inc</a>
      </h4>
      <span class="job-search-card__location">New York, NY</span>
      <a href="https://www.linkedin.com/jobs/view/67890?trk=xyz">View</a>
    </div>
  </li>
  <li>
    <div class="base-card job-search-card">
      <h3 class="base-search-card__title"></h3>
    </div>
  </li>
</ul>
</body></html>
"""

EMPTY_LINKEDIN_HTML = """
<html><body>
<div class="no-results">No matching jobs found</div>
</body></html>
"""


def _make_profile() -> SiteProfile:
    return SiteProfile(
        site_id="linkedin",
        base_url="https://www.linkedin.com",
        auth=AuthConfig(type=AuthType.NONE),
        rate_limit=RateLimitConfig(min_delay_s=2.0),
        navigation=NavigationConfig(
            search_url_template="https://www.linkedin.com/jobs/search/?keywords={query}",
            pagination=PaginationStrategy.INFINITE_SCROLL,
        ),
        selectors=SelectorConfig(
            result_item="div.job-search-card",
            field_map={
                "title": "h3.base-search-card__title",
                "link": "a[href*='/jobs/']",
            },
        ),
        output_contract=OutputContract(
            fields=[
                FieldSpec(name="title", type="str", required=True),
                FieldSpec(name="url", type="url", required=False),
                FieldSpec(name="company", type="str", required=False),
                FieldSpec(name="location", type="str", required=False),
            ],
            dedup_fields=["title", "url"],
            min_records_per_search=1,
        ),
    )


class TestLinkedInExtraction:
    def test_extracts_job_cards(self) -> None:
        profile = _make_profile()
        actions = MagicMock()
        wrapper = LinkedInWrapper(profile, actions)
        records = wrapper.extract_from_fixture(LINKEDIN_JOB_CARDS_HTML)

        assert len(records) == 2
        assert records[0]["title"] == "Senior Software Engineer"
        assert records[0]["company"] == "Acme Corp"
        assert records[0]["location"] == "San Francisco, CA"
        assert "/jobs/view/12345" in records[0]["url"]

        assert records[1]["title"] == "Backend Developer"
        assert records[1]["company"] == "Beta Inc"

    def test_empty_page_returns_empty(self) -> None:
        profile = _make_profile()
        actions = MagicMock()
        wrapper = LinkedInWrapper(profile, actions)
        records = wrapper.extract_from_fixture(EMPTY_LINKEDIN_HTML)

        assert len(records) == 0

    def test_skips_cards_without_title(self) -> None:
        profile = _make_profile()

        html = """
        <html><body>
        <div class="base-card job-search-card">
          <h3 class="base-search-card__title"></h3>
        </div>
        <div class="base-card job-search-card">
          <h3 class="base-search-card__title">Real Job</h3>
          <a href="https://www.linkedin.com/jobs/view/999">View</a>
        </div>
        </body></html>
        """

        actions = MagicMock()
        wrapper = LinkedInWrapper(profile, actions)
        records = wrapper.extract_from_fixture(html)

        assert len(records) == 1
        assert records[0]["title"] == "Real Job"
