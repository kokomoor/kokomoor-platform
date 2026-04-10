"""Pydantic models for the Universal Scraper Pipeline.

Every model in this module is domain-agnostic — no job-specific, property-
specific, or site-specific assumptions.  Site-specific behavior comes from
the ``SiteProfile`` and ``OutputContract`` that callers provide.
"""

from __future__ import annotations

import uuid
import warnings
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from pydantic.config import ConfigDict

# ---------------------------------------------------------------------------
# Output contract — describes what a site should yield
# ---------------------------------------------------------------------------


class FieldSpec(BaseModel):
    """Specification for a single field in the output contract."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["str", "int", "float", "bool", "date", "url", "list[str]"] = "str"
    required: bool = True
    description: str = ""


class OutputContract(BaseModel):
    """Defines what well-formed output looks like for a given site."""

    model_config = ConfigDict(extra="forbid")

    fields: list[FieldSpec]
    dedup_fields: list[str] = Field(
        description="Subset of field names whose values compose the dedup key.",
    )
    min_records_per_search: int = Field(
        default=1, ge=0, description="SLO: expect at least this many records per search."
    )
    max_empty_pages_before_stop: int = Field(
        default=3, ge=1, description="Halt pagination after this many consecutive empty pages."
    )


# ---------------------------------------------------------------------------
# Authentication configuration
# ---------------------------------------------------------------------------


class AuthType(StrEnum):
    NONE = "none"
    CREDENTIAL_FORM = "credential_form"
    SESSION_COOKIE = "session_cookie"
    API_KEY = "api_key"
    OAUTH = "oauth"


class AuthConfig(BaseModel):
    """How to authenticate with a site."""

    model_config = ConfigDict(extra="forbid")

    type: AuthType = AuthType.NONE
    env_username_key: str = Field(default="", description="KP_ env var name for the username.")
    env_password_key: str = Field(default="", description="KP_ env var name for the password.")
    login_url: str = ""
    username_selector: str = ""
    password_selector: str = ""
    submit_selector: str = ""
    success_indicator: str = Field(
        default="", description="CSS selector or URL fragment proving auth succeeded."
    )
    mfa_strategy: Literal["none", "totp", "pause_notify"] = "none"

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_auth_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        migrated = dict(data)
        legacy_type = migrated.get("type")
        if legacy_type == "credential":
            warnings.warn(
                "AuthConfig.type='credential' is deprecated; use 'credential_form'.",
                stacklevel=2,
            )
            migrated["type"] = "credential_form"

        prefix = migrated.pop("credential_env_prefix", "")
        if prefix and not migrated.get("env_username_key") and not migrated.get("env_password_key"):
            warnings.warn(
                "AuthConfig.credential_env_prefix is deprecated; use env_username_key/env_password_key.",
                stacklevel=2,
            )
            prefix_str = str(prefix).strip()
            if prefix_str.startswith("KP_"):
                prefix_str = prefix_str[3:]
            migrated["env_username_key"] = f"{prefix_str}_EMAIL"
            migrated["env_password_key"] = f"{prefix_str}_PASSWORD"

        return migrated


# ---------------------------------------------------------------------------
# Navigation & selectors
# ---------------------------------------------------------------------------


class PaginationStrategy(StrEnum):
    NEXT_BUTTON = "next_button"
    URL_PARAMETER = "url_parameter"
    INFINITE_SCROLL = "infinite_scroll"
    ASPNET_POSTBACK = "aspnet_postback"
    NONE = "none"


class NavigationConfig(BaseModel):
    """How to navigate search results on the site."""

    model_config = ConfigDict(extra="forbid")

    search_url_template: str = Field(
        description="Python format-string with ``{query}``, ``{page}``, etc. placeholders."
    )
    pagination: PaginationStrategy = PaginationStrategy.NEXT_BUTTON
    next_button_selector: str = ""
    page_param_name: str = "page"
    results_container_selector: str = ""
    no_results_indicator: str = ""

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_navigation_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        migrated = dict(data)
        if "pagination_strategy" in migrated and "pagination" not in migrated:
            warnings.warn(
                "NavigationConfig.pagination_strategy is deprecated; use pagination.",
                stacklevel=2,
            )
            migrated["pagination"] = migrated.pop("pagination_strategy")

        if "page_param" in migrated and "page_param_name" not in migrated:
            warnings.warn(
                "NavigationConfig.page_param is deprecated; use page_param_name.",
                stacklevel=2,
            )
            migrated["page_param_name"] = migrated.pop("page_param")

        return migrated


class SelectorConfig(BaseModel):
    """CSS/XPath selectors for extracting data from result pages."""

    model_config = ConfigDict(extra="forbid")

    result_item: str = Field(description="Selector for each result row/card on the listing page.")
    field_map: dict[str, str] = Field(
        default_factory=dict,
        description="Map of output field name → CSS selector relative to the result item.",
    )
    detail_link: str = Field(default="", description="Selector for the link to a detail page.")
    detail_field_map: dict[str, str] = Field(
        default_factory=dict,
        description="Map of field name → selector on the detail page (optional enrichment).",
    )


# ---------------------------------------------------------------------------
# Rate-limit profile (mirrors core.browser.rate_limiter.RateLimitProfile)
# ---------------------------------------------------------------------------


class RateLimitConfig(BaseModel):
    """Rate-limit profile embedded in a site profile."""

    model_config = ConfigDict(extra="forbid")

    min_delay_s: float = 4.0
    max_delay_s: float = 10.0
    pages_before_long_pause: int = 8
    long_pause_min_s: float = 35.0
    long_pause_max_s: float = 90.0


# ---------------------------------------------------------------------------
# Site profile — the top-level descriptor for any target site
# ---------------------------------------------------------------------------


class SiteProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    """Complete description of a scraping target.

    A ``SiteProfile`` is all a ``BaseSiteWrapper`` needs to authenticate,
    navigate, extract, and validate results from a site.  Thin per-site
    wrapper subclasses may override individual methods but the profile
    drives the default implementation.
    """

    site_id: str = Field(description="Unique slug, e.g. 'linkedin', 'vision_gsi_woonsocket'.")
    display_name: str = ""
    base_url: str
    auth: AuthConfig = Field(default_factory=AuthConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    requires_browser: bool = Field(
        default=False, description="Skip HTTP-first and go straight to browser."
    )
    navigation: NavigationConfig
    selectors: SelectorConfig
    output_contract: OutputContract
    fixture_refresh_days: int = Field(default=7, ge=1)
    drift_threshold: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="Structural similarity below which the site is flagged as 'drifted'.",
    )
    max_pages_per_search: int = Field(default=20, ge=1)
    notes: str = ""
    proxy: str = Field(default="", description="Optional per-site proxy URL.")


# ---------------------------------------------------------------------------
# Request / result / report models
# ---------------------------------------------------------------------------


class ScrapeRequest(BaseModel):
    """Input to the scrape node."""

    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    site_id: str
    search_params: dict[str, Any] = Field(default_factory=dict)
    max_records: int = 500
    max_pages: int | None = None


class ErrorClassification(StrEnum):
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    SELECTOR = "selector"
    TIMEOUT = "timeout"
    CAPTCHA = "captcha"
    NETWORK = "network"
    EXTRACTION = "extraction"
    UNKNOWN = "unknown"


class ScrapeError(BaseModel):
    """Structured error from a scrape operation."""

    classification: ErrorClassification
    message: str
    stage: str = ""
    recoverable: bool = True


class TimingBreakdown(BaseModel):
    """Per-stage timing in milliseconds."""

    auth_ms: float = 0.0
    search_ms: float = 0.0
    extract_ms: float = 0.0
    paginate_ms: float = 0.0
    dedup_ms: float = 0.0
    store_ms: float = 0.0
    total_ms: float = 0.0


class DedupStats(BaseModel):
    """Deduplication statistics for a scrape run."""

    total_extracted: int = 0
    new_records: int = 0
    duplicates_skipped: int = 0
    bloom_checks: int = 0
    sqlite_checks: int = 0


class ScrapeResult(BaseModel):
    """Output from the scrape node."""

    run_id: str
    site_id: str
    records: list[dict[str, Any]] = Field(default_factory=list)
    dedup_stats: DedupStats = Field(default_factory=DedupStats)
    timing: TimingBreakdown = Field(default_factory=TimingBreakdown)
    errors: list[ScrapeError] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    pages_visited: int = 0
    drift_detected: bool = False
    fingerprint_similarity: float | None = None


class ScrapeReport(BaseModel):
    """Aggregated metrics for a full scrape run (may span multiple searches)."""

    run_id: str
    site_id: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    total_records_found: int = 0
    total_new_records: int = 0
    total_duplicates: int = 0
    total_pages: int = 0
    total_errors: int = 0
    error_breakdown: dict[str, int] = Field(default_factory=dict)
    timing: TimingBreakdown = Field(default_factory=TimingBreakdown)
    drift_detected: bool = False


# ---------------------------------------------------------------------------
# Validation report
# ---------------------------------------------------------------------------


class FieldViolation(BaseModel):
    """A single field-level validation failure."""

    record_index: int
    field_name: str
    expected_type: str
    actual_value: str
    message: str


class ValidationReport(BaseModel):
    """Comprehensive validation output run after every scrape."""

    run_id: str
    site_id: str
    schema_valid: bool = True
    field_violations: list[FieldViolation] = Field(default_factory=list)
    coverage_met: bool = True
    records_found: int = 0
    records_expected: int = 0
    dedup_integrity: bool = True
    duplicate_keys_found: list[str] = Field(default_factory=list)
    drift_detected: bool = False
    fingerprint_similarity: float | None = None
    fixture_age_days: float | None = None
    fixture_stale: bool = False
    passed: bool = True
    summary: str = ""


# ---------------------------------------------------------------------------
# Remediation models (used by heal node in Pass 3)
# ---------------------------------------------------------------------------


class RemediationStep(BaseModel):
    """One atomic instruction in a remediation plan."""

    order: int
    file_path: str
    action: Literal["edit", "add", "delete", "rename"]
    description: str
    before_pattern: str | None = None
    after_guidance: str
    rationale: str
    constraints: list[str] = Field(default_factory=list)


class RemediationReport(BaseModel):
    """Structured diagnosis + repair plan produced by the heal node."""

    heal_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    site_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    diagnosis: str
    severity: Literal["low", "medium", "high", "critical"]
    confidence: float = Field(ge=0.0, le=1.0)
    root_cause: str
    affected_files: list[str]
    steps: list[RemediationStep]
    guardrails: list[str] = Field(default_factory=list)
    test_plan: str = ""
    estimated_tokens: int = 0
    fresh_fixture_path: str = ""
