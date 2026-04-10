"""Comprehensive validation node.

Runs after every scrape to produce a ``ValidationReport`` covering:
- Schema compliance (field presence, types)
- Coverage SLOs (minimum records expected)
- Dedup integrity (no duplicate keys in the batch)
- Drift detection (fingerprint comparison)
- Fixture staleness check
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from core.scraper.dedup import compute_dedup_key
from pipelines.scraper.models import (
    FieldViolation,
    OutputContract,
    ScrapeResult,
    SiteProfile,
    ValidationReport,
)

if TYPE_CHECKING:
    from core.scraper.fixtures import FixtureStore

logger = structlog.get_logger(__name__)

_TYPE_VALIDATORS: dict[str, type | tuple[type, ...]] = {
    "str": (str,),
    "int": (int,),
    "float": (int, float),
    "bool": (bool,),
    "date": (str,),
    "url": (str,),
    "list[str]": (list,),
}


def validate_result(
    result: ScrapeResult,
    profile: SiteProfile,
    *,
    fixture_store: FixtureStore | None = None,
) -> ValidationReport:
    """Run comprehensive validation on a scrape result.

    Returns a ``ValidationReport`` — callers decide whether to act on failures.
    """
    contract = profile.output_contract
    report = ValidationReport(
        run_id=result.run_id,
        site_id=profile.site_id,
        records_found=len(result.records),
        records_expected=contract.min_records_per_search,
        drift_detected=result.drift_detected,
        fingerprint_similarity=result.fingerprint_similarity,
    )

    _validate_schema(result.records, contract, report)
    _validate_coverage(result, contract, report)
    _validate_dedup_integrity(result.records, contract, report)

    if fixture_store:
        _validate_fixture_freshness(profile, fixture_store, report)

    report.passed = (
        report.schema_valid
        and report.coverage_met
        and report.dedup_integrity
        and not report.drift_detected
        and not report.fixture_stale
    )

    report.summary = _build_summary(report)

    logger.info(
        "validate.complete",
        run_id=result.run_id,
        site_id=profile.site_id,
        passed=report.passed,
        records=report.records_found,
        violations=len(report.field_violations),
        drift=report.drift_detected,
    )
    return report


def _validate_schema(
    records: list[dict[str, Any]],
    contract: OutputContract,
    report: ValidationReport,
) -> None:
    """Check that every record conforms to the output contract's field specs."""
    for idx, record in enumerate(records):
        for spec in contract.fields:
            value = record.get(spec.name)

            if value is None or (isinstance(value, str) and not value.strip()):
                if spec.required:
                    report.field_violations.append(
                        FieldViolation(
                            record_index=idx,
                            field_name=spec.name,
                            expected_type=spec.type,
                            actual_value="<missing>",
                            message=f"Required field '{spec.name}' is missing or empty",
                        )
                    )
                continue

            expected_types = _TYPE_VALIDATORS.get(spec.type, (str,))
            if not isinstance(value, expected_types):
                report.field_violations.append(
                    FieldViolation(
                        record_index=idx,
                        field_name=spec.name,
                        expected_type=spec.type,
                        actual_value=str(value)[:100],
                        message=f"Type mismatch: expected {spec.type}, got {type(value).__name__}",
                    )
                )

    if report.field_violations:
        report.schema_valid = False


def _validate_coverage(
    result: ScrapeResult,
    contract: OutputContract,
    report: ValidationReport,
) -> None:
    """Check if the scrape met minimum record count SLOs."""
    if len(result.records) < contract.min_records_per_search:
        report.coverage_met = False
        logger.warning(
            "validate.coverage_below_slo",
            site_id=result.site_id,
            found=len(result.records),
            expected=contract.min_records_per_search,
        )


def _validate_dedup_integrity(
    records: list[dict[str, Any]],
    contract: OutputContract,
    report: ValidationReport,
) -> None:
    """Check that no duplicate dedup keys exist within this batch."""
    seen: dict[str, int] = {}
    for idx, record in enumerate(records):
        key = compute_dedup_key(record, contract.dedup_fields)
        if key in seen:
            report.duplicate_keys_found.append(key)
            report.dedup_integrity = False
        else:
            seen[key] = idx


def _validate_fixture_freshness(
    profile: SiteProfile,
    fixture_store: FixtureStore,
    report: ValidationReport,
) -> None:
    """Check if offline fixtures are stale."""
    age = fixture_store.fixture_age_days(profile.site_id)
    report.fixture_age_days = age
    if age is None or age > profile.fixture_refresh_days:
        report.fixture_stale = True
        logger.warning(
            "validate.fixture_stale",
            site_id=profile.site_id,
            age_days=age,
            max_age=profile.fixture_refresh_days,
        )


def _build_summary(report: ValidationReport) -> str:
    """Build a human-readable summary line."""
    parts: list[str] = []

    if report.passed:
        parts.append("PASSED")
    else:
        parts.append("FAILED")

    parts.append(f"records={report.records_found}")

    if not report.schema_valid:
        parts.append(f"schema_violations={len(report.field_violations)}")
    if not report.coverage_met:
        parts.append(f"coverage_below_slo(expected={report.records_expected})")
    if not report.dedup_integrity:
        parts.append(f"dup_keys={len(report.duplicate_keys_found)}")
    if report.drift_detected:
        parts.append(f"drift(similarity={report.fingerprint_similarity})")
    if report.fixture_stale:
        parts.append(f"stale_fixtures(age={report.fixture_age_days}d)")

    return " | ".join(parts)
