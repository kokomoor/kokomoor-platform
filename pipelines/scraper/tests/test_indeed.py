"""Offline fixture tests for IndeedWrapper."""

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
from pipelines.scraper.wrappers.indeed import IndeedWrapper

INDEED_RESULTS_HTML = """
<html><body>
<div id="mosaic-provider-jobcards">
  <ul>
    <li>
      <div class="job_seen_beacon">
        <h2 class="jobTitle"><a data-jk="abc123" href="/rc/clk?jk=abc123">Data Analyst</a></h2>
        <span class="companyName">DataCo</span>
        <div class="companyLocation">Boston, MA</div>
        <div class="salary-snippet-container">$80,000 - $100,000</div>
      </div>
    </li>
    <li>
      <div class="job_seen_beacon">
        <h2 class="jobTitle"><a data-jk="def456" href="/rc/clk?jk=def456">ML Engineer</a></h2>
        <span class="companyName">AI Labs</span>
        <div class="companyLocation">Remote</div>
      </div>
    </li>
    <li>
      <div class="job_seen_beacon">
        <h2 class="jobTitle"><a href="#">  </a></h2>
      </div>
    </li>
  </ul>
</div>
</body></html>
"""

EMPTY_INDEED_HTML = """
<html><body>
<div class="jobsearch-NoResult-messageContainer">
  <p>The search did not return any results.</p>
</div>
</body></html>
"""


def _make_profile() -> SiteProfile:
    return SiteProfile(
        site_id="indeed",
        base_url="https://www.indeed.com",
        auth=AuthConfig(type=AuthType.NONE),
        rate_limit=RateLimitConfig(min_delay_s=1.0),
        navigation=NavigationConfig(
            search_url_template="https://www.indeed.com/jobs?q={query}&l={location}",
            pagination=PaginationStrategy.URL_PARAMETER,
            page_param_name="start",
        ),
        selectors=SelectorConfig(
            result_item="div.job_seen_beacon",
            field_map={
                "title": "h2.jobTitle a",
                "link": "a[data-jk]",
            },
        ),
        output_contract=OutputContract(
            fields=[
                FieldSpec(name="title", type="str", required=True),
                FieldSpec(name="url", type="url", required=False),
                FieldSpec(name="company", type="str", required=False),
                FieldSpec(name="location", type="str", required=False),
                FieldSpec(name="salary", type="str", required=False),
            ],
            dedup_fields=["title", "url"],
            min_records_per_search=1,
        ),
    )


class TestIndeedExtraction:
    def test_extracts_job_cards(self) -> None:
        profile = _make_profile()
        actions = MagicMock()
        wrapper = IndeedWrapper(profile, actions)
        records = wrapper.extract_from_fixture(INDEED_RESULTS_HTML)

        assert len(records) == 2
        assert records[0]["title"] == "Data Analyst"
        assert records[0]["company"] == "DataCo"
        assert records[0]["location"] == "Boston, MA"
        assert records[0]["salary"] == "$80,000 - $100,000"
        assert "abc123" in records[0]["url"]

        assert records[1]["title"] == "ML Engineer"
        assert records[1]["company"] == "AI Labs"
        assert "salary" not in records[1]

    def test_empty_results(self) -> None:
        profile = _make_profile()
        actions = MagicMock()
        wrapper = IndeedWrapper(profile, actions)
        records = wrapper.extract_from_fixture(EMPTY_INDEED_HTML)

        assert len(records) == 0

    def test_skips_whitespace_only_titles(self) -> None:
        profile = _make_profile()
        actions = MagicMock()
        wrapper = IndeedWrapper(profile, actions)
        records = wrapper.extract_from_fixture(INDEED_RESULTS_HTML)

        for rec in records:
            assert rec["title"].strip() != ""
