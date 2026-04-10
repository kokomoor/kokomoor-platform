"""Data contracts for the discovery subsystem.

ListingRef  — minimal listing data extracted from a search result card.
              Lightweight — no full description, just what's visible in a SERP.
DiscoveryConfig — per-run configuration for the orchestrator.
ProviderResult  — result bundle returned by each provider adapter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, SecretStr

from pipelines.job_agent.models import ApplicationStatus, JobSource

if TYPE_CHECKING:
    from core.config import Settings
    from pipelines.job_agent.models import JobListing

from pipelines.job_agent.discovery.deduplication import compute_dedup_key

# ---------------------------------------------------------------------------
# Lightweight discovery data types (frozen dataclasses, not DB models)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ListingRef:
    """Minimal listing data extracted from a search result card."""

    url: str
    title: str
    company: str
    source: JobSource
    location: str = ""
    salary_text: str = ""
    raw_card_text: str = ""


@dataclass(frozen=True)
class ParsedSalary:
    """Parsed salary range in USD."""

    min_usd: int | None
    max_usd: int | None


@dataclass(frozen=True)
class ProviderResult:
    """Result bundle returned by each provider adapter."""

    source: JobSource
    refs: list[ListingRef]
    errors: list[str]
    pages_scraped: int
    session_saved: bool


# ---------------------------------------------------------------------------
# Salary parsing
# ---------------------------------------------------------------------------

_HOURLY_RE = re.compile(r"/\s*h(?:ou)?r", re.IGNORECASE)
_K_VALUE_RE = re.compile(r"\$\s*([\d,.]+)\s*[kK]")
_FULL_VALUE_RE = re.compile(r"\$\s*([\d,]+)")


def _parse_dollar(raw: str) -> int | None:
    """Convert a dollar string fragment to an integer, handling K suffix."""
    raw = raw.replace(",", "")
    try:
        val = float(raw)
    except ValueError:
        return None
    return int(val)


def parse_salary_text(text: str) -> ParsedSalary:
    """Parse common salary string formats into integer USD values."""
    if not text or _HOURLY_RE.search(text):
        return ParsedSalary(None, None)

    k_matches = _K_VALUE_RE.findall(text)
    if k_matches:
        values = [int(float(m.replace(",", "")) * 1000) for m in k_matches]
        if "up to" in text.lower():
            return ParsedSalary(None, values[-1])
        if len(values) >= 2:
            return ParsedSalary(min(values), max(values))
        if "+" in text:
            return ParsedSalary(values[0], None)
        return ParsedSalary(values[0], None)

    full_matches = _FULL_VALUE_RE.findall(text)
    if full_matches:
        values = [v for m in full_matches if (v := _parse_dollar(m)) is not None]
        if not values:
            return ParsedSalary(None, None)
        if "up to" in text.lower():
            return ParsedSalary(None, values[-1])
        if len(values) >= 2:
            return ParsedSalary(min(values), max(values))
        if "+" in text:
            return ParsedSalary(values[0], None)
        return ParsedSalary(values[0], None)

    return ParsedSalary(None, None)


# ---------------------------------------------------------------------------
# ListingRef → JobListing conversion
# ---------------------------------------------------------------------------


def ref_to_job_listing(ref: ListingRef) -> JobListing:
    """Convert a lightweight ListingRef to a persistable JobListing."""
    from pipelines.job_agent.models import JobListing

    salary = parse_salary_text(ref.salary_text)
    return JobListing(
        title=ref.title,
        company=ref.company,
        location=ref.location,
        url=ref.url,
        source=ref.source,
        salary_min=salary.min_usd,
        salary_max=salary.max_usd,
        status=ApplicationStatus.DISCOVERED,
        dedup_key=compute_dedup_key(ref.company, ref.title, ref.url),
    )


# ---------------------------------------------------------------------------
# Discovery run configuration
# ---------------------------------------------------------------------------


class DiscoveryConfig(BaseModel):
    """Per-run configuration for the discovery orchestrator."""

    sessions_dir: str
    max_concurrent_providers: int = 2
    max_pages_per_search: int = 8
    max_listings_per_provider: int = 150
    session_max_age_hours: int = 72
    prefilter_min_score: float = 0.0

    linkedin_enabled: bool = True
    indeed_enabled: bool = True
    builtin_enabled: bool = True
    wellfound_enabled: bool = False
    greenhouse_enabled: bool = True
    lever_enabled: bool = True
    workday_enabled: bool = False

    greenhouse_companies: list[str] = Field(default_factory=list)
    lever_companies: list[str] = Field(default_factory=list)
    workday_companies: list[str] = Field(default_factory=list)

    direct_site_configs: str = ""

    captcha_strategy: Literal["avoid", "pause_notify", "solve"] = "pause_notify"
    captcha_api_key: SecretStr = SecretStr("")

    @classmethod
    def from_settings(cls, settings: Settings) -> DiscoveryConfig:
        """Build DiscoveryConfig from the global Settings singleton."""
        return cls(
            sessions_dir=settings.discovery_sessions_dir,
            max_concurrent_providers=settings.discovery_max_concurrent_providers,
            max_pages_per_search=settings.discovery_max_pages_per_search,
            max_listings_per_provider=settings.discovery_max_listings_per_provider,
            session_max_age_hours=settings.discovery_session_max_age_hours,
            prefilter_min_score=settings.discovery_prefilter_min_score,
            linkedin_enabled=settings.discovery_linkedin_enabled,
            indeed_enabled=settings.discovery_indeed_enabled,
            builtin_enabled=settings.discovery_builtin_enabled,
            wellfound_enabled=settings.discovery_wellfound_enabled,
            greenhouse_enabled=settings.discovery_greenhouse_enabled,
            lever_enabled=settings.discovery_lever_enabled,
            workday_enabled=settings.discovery_workday_enabled,
            greenhouse_companies=settings.greenhouse_company_list,
            lever_companies=settings.lever_company_list,
            workday_companies=settings.workday_company_list,
            direct_site_configs=settings.direct_site_configs,
            captcha_strategy=settings.captcha_strategy,
            captcha_api_key=settings.captcha_api_key,
        )
