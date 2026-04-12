"""Tests for the validation node — schema, coverage, dedup integrity, drift."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pipelines.scraper.models import (
    ScrapeResult,
    SiteProfile,
)
from pipelines.scraper.nodes.validate import validate_result

if TYPE_CHECKING:
    from core.scraper.fixtures import FixtureStore


def _make_result(
    records: list[dict[str, Any]],
    *,
    site_id: str = "test_site",
    drift: bool = False,
    similarity: float | None = None,
) -> ScrapeResult:
    return ScrapeResult(
        run_id="test_run",
        site_id=site_id,
        records=records,
        drift_detected=drift,
        fingerprint_similarity=similarity,
    )


class TestSchemaValidation:
    def test_valid_records_pass(self, sample_profile: SiteProfile) -> None:
        records = [
            {"title": "A", "url": "https://x.com", "description": "d", "price": "10"},
            {"title": "B", "url": "https://y.com", "description": "", "price": ""},
        ]
        result = _make_result(records)
        report = validate_result(result, sample_profile)
        assert report.schema_valid
        assert report.passed

    def test_missing_required_field_fails(self, sample_profile: SiteProfile) -> None:
        records = [{"url": "https://x.com", "description": "d"}]
        result = _make_result(records)
        report = validate_result(result, sample_profile)
        assert not report.schema_valid
        assert len(report.field_violations) >= 1
        assert any(v.field_name == "title" for v in report.field_violations)

    def test_empty_required_field_fails(self, sample_profile: SiteProfile) -> None:
        records = [{"title": "", "url": "https://x.com"}]
        result = _make_result(records)
        report = validate_result(result, sample_profile)
        assert not report.schema_valid

    def test_optional_field_missing_ok(self, sample_profile: SiteProfile) -> None:
        records = [{"title": "A", "url": "https://x.com"}]
        result = _make_result(records)
        report = validate_result(result, sample_profile)
        assert report.schema_valid


class TestCoverageValidation:
    def test_meets_slo(self, sample_profile: SiteProfile) -> None:
        records = [{"title": "A", "url": "https://x.com"}]
        result = _make_result(records)
        report = validate_result(result, sample_profile)
        assert report.coverage_met

    def test_below_slo(self, sample_profile: SiteProfile) -> None:
        result = _make_result([])
        report = validate_result(result, sample_profile)
        assert not report.coverage_met


class TestDedupIntegrity:
    def test_unique_records_pass(self, sample_profile: SiteProfile) -> None:
        records = [
            {"title": "A", "url": "https://a.com"},
            {"title": "B", "url": "https://b.com"},
        ]
        result = _make_result(records)
        report = validate_result(result, sample_profile)
        assert report.dedup_integrity

    def test_duplicate_keys_fail(self, sample_profile: SiteProfile) -> None:
        records = [
            {"title": "A", "url": "https://a.com"},
            {"title": "A", "url": "https://a.com"},
        ]
        result = _make_result(records)
        report = validate_result(result, sample_profile)
        assert not report.dedup_integrity
        assert len(report.duplicate_keys_found) >= 1


class TestDriftDetection:
    def test_no_drift_passes(self, sample_profile: SiteProfile) -> None:
        result = _make_result([{"title": "A", "url": "https://x.com"}])
        report = validate_result(result, sample_profile)
        assert not report.drift_detected
        assert report.passed

    def test_drift_detected_fails(self, sample_profile: SiteProfile) -> None:
        result = _make_result(
            [{"title": "A", "url": "https://x.com"}],
            drift=True,
            similarity=0.65,
        )
        report = validate_result(result, sample_profile)
        assert report.drift_detected
        assert not report.passed


class TestFixtureFreshness:
    def test_no_fixtures_is_stale(
        self, sample_profile: SiteProfile, tmp_fixture_store: FixtureStore
    ) -> None:
        result = _make_result([{"title": "A", "url": "https://x.com"}])
        report = validate_result(result, sample_profile, fixture_store=tmp_fixture_store)
        assert report.fixture_stale


class TestSummary:
    def test_passed_summary(self, sample_profile: SiteProfile) -> None:
        records = [{"title": "A", "url": "https://x.com"}]
        result = _make_result(records)
        report = validate_result(result, sample_profile)
        assert "PASSED" in report.summary

    def test_failed_summary_includes_reasons(self, sample_profile: SiteProfile) -> None:
        result = _make_result([], drift=True, similarity=0.5)
        report = validate_result(result, sample_profile)
        assert "FAILED" in report.summary
        assert "drift" in report.summary
        assert "coverage" in report.summary
