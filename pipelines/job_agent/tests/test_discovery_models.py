"""Tests for discovery models: ref_to_job_listing, DiscoveryConfig.from_settings."""

from __future__ import annotations

from unittest.mock import MagicMock

from pydantic import SecretStr

from pipelines.job_agent.discovery.models import (
    DiscoveryConfig,
    ListingRef,
    parse_salary_text,
    ref_to_job_listing,
)
from pipelines.job_agent.models import ApplicationStatus, JobSource


class TestRefToJobListing:
    def test_maps_all_fields(self) -> None:
        ref = ListingRef(
            url="https://example.com/jobs/1?utm_source=test&id=123#apply",
            title="Software Engineer",
            company="Acme Corp",
            source=JobSource.LINKEDIN,
            location="San Francisco, CA",
            salary_text="$180K - $240K",
        )
        listing = ref_to_job_listing(ref)

        assert listing.title == "Software Engineer"
        assert listing.company == "Acme Corp"
        assert listing.location == "San Francisco, CA"
        assert listing.url == "https://example.com/jobs/1?id=123"
        assert listing.source == JobSource.LINKEDIN
        assert listing.status == ApplicationStatus.DISCOVERED
        assert listing.salary_min == 180_000
        assert listing.salary_max == 240_000
        assert listing.dedup_key
        assert len(listing.dedup_key) == 32

    def test_no_salary_text(self) -> None:
        ref = ListingRef(
            url="https://example.com/2",
            title="PM",
            company="Co",
            source=JobSource.INDEED,
        )
        listing = ref_to_job_listing(ref)
        assert listing.salary_min is None
        assert listing.salary_max is None

    def test_hourly_salary_ignored(self) -> None:
        ref = ListingRef(
            url="https://example.com/3",
            title="Contractor",
            company="Co",
            source=JobSource.OTHER,
            salary_text="$75/hr",
        )
        listing = ref_to_job_listing(ref)
        assert listing.salary_min is None
        assert listing.salary_max is None

    def test_dedup_key_deterministic(self) -> None:
        ref = ListingRef(
            url="https://example.com/1",
            title="SWE",
            company="Acme",
            source=JobSource.GREENHOUSE,
        )
        listing1 = ref_to_job_listing(ref)
        listing2 = ref_to_job_listing(ref)
        assert listing1.dedup_key == listing2.dedup_key

    def test_description_is_empty(self) -> None:
        ref = ListingRef(
            url="https://example.com/1",
            title="SWE",
            company="Acme",
            source=JobSource.LEVER,
        )
        listing = ref_to_job_listing(ref)
        assert listing.description == ""


class TestParseSalaryTextExhaustive:
    def test_k_range_dash(self) -> None:
        assert parse_salary_text("$180K - $240K").min_usd == 180_000
        assert parse_salary_text("$180K - $240K").max_usd == 240_000

    def test_k_range_en_dash(self) -> None:
        result = parse_salary_text("$150K \u2013 $200K")
        assert result.min_usd == 150_000
        assert result.max_usd == 200_000

    def test_comma_separated_range(self) -> None:
        result = parse_salary_text("$180,000 - $240,000")
        assert result.min_usd == 180_000
        assert result.max_usd == 240_000

    def test_single_k_plus(self) -> None:
        result = parse_salary_text("$200K+")
        assert result.min_usd == 200_000
        assert result.max_usd is None

    def test_up_to(self) -> None:
        result = parse_salary_text("Up to $250K")
        assert result.min_usd is None
        assert result.max_usd == 250_000

    def test_hourly_returns_none(self) -> None:
        result = parse_salary_text("$55/hr")
        assert result.min_usd is None
        assert result.max_usd is None

    def test_hourly_full_word(self) -> None:
        result = parse_salary_text("$55/hour")
        assert result.min_usd is None
        assert result.max_usd is None

    def test_unrecognizable(self) -> None:
        result = parse_salary_text("Competitive")
        assert result.min_usd is None
        assert result.max_usd is None

    def test_empty_string(self) -> None:
        result = parse_salary_text("")
        assert result.min_usd is None
        assert result.max_usd is None


class TestDiscoveryConfigFromSettings:
    def test_maps_settings_fields(self) -> None:
        mock_settings = MagicMock()
        mock_settings.discovery_sessions_dir = "/tmp/sessions"
        mock_settings.discovery_max_concurrent_providers = 3
        mock_settings.discovery_max_pages_per_search = 5
        mock_settings.discovery_max_listings_per_provider = 100
        mock_settings.discovery_session_max_age_hours = 48
        mock_settings.discovery_prefilter_min_score = 0.3
        mock_settings.discovery_linkedin_enabled = True
        mock_settings.discovery_indeed_enabled = False
        mock_settings.discovery_builtin_enabled = True
        mock_settings.discovery_wellfound_enabled = False
        mock_settings.discovery_greenhouse_enabled = True
        mock_settings.discovery_lever_enabled = True
        mock_settings.discovery_workday_enabled = False
        mock_settings.greenhouse_company_list = ["acme", "widgets"]
        mock_settings.lever_company_list = ["openai"]
        mock_settings.workday_company_list = []
        mock_settings.direct_site_configs = ""
        mock_settings.captcha_strategy = "pause_notify"
        mock_settings.captcha_api_key = SecretStr("test-key")

        config = DiscoveryConfig.from_settings(mock_settings)

        assert config.sessions_dir == "/tmp/sessions"
        assert config.max_concurrent_providers == 3
        assert config.max_pages_per_search == 5
        assert config.max_listings_per_provider == 100
        assert config.prefilter_min_score == 0.3
        assert config.indeed_enabled is False
        assert config.greenhouse_companies == ["acme", "widgets"]
        assert config.lever_companies == ["openai"]
        assert config.captcha_api_key.get_secret_value() == "test-key"
