"""Core scrape execution node.

Orchestrates the full scrape lifecycle: HTTP attempt → browser fallback →
extraction → dedup → content store.  Produces a ``ScrapeResult`` with
per-stage timing, error classification, and runtime drift detection.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from core.browser import BrowserManager
from core.browser.actions import BrowserActions
from core.browser.human_behavior import HumanBehavior
from core.scraper.dedup import DedupEngine, compute_dedup_key
from core.scraper.http_client import StealthHttpClient
from pipelines.scraper.models import (
    ErrorClassification,
    ScrapeError,
    ScrapeRequest,
    ScrapeResult,
    SiteProfile,
    TimingBreakdown,
)
from pipelines.scraper.wrappers.base import BaseSiteWrapper

if TYPE_CHECKING:
    from core.scraper.content_store import ContentStore
    from core.scraper.fixtures import FixtureStore

logger = structlog.get_logger(__name__)

_WRAPPER_REGISTRY: dict[str, type[BaseSiteWrapper]] = {}


def _build_registry() -> dict[str, type[BaseSiteWrapper]]:
    """Lazily build the site_id → wrapper class registry."""
    if _WRAPPER_REGISTRY:
        return _WRAPPER_REGISTRY

    from pipelines.scraper.wrappers.indeed import IndeedWrapper
    from pipelines.scraper.wrappers.linkedin import LinkedInWrapper
    from pipelines.scraper.wrappers.uslandrecords import USLandRecordsWrapper
    from pipelines.scraper.wrappers.vision_gsi import VisionGSIWrapper

    _WRAPPER_REGISTRY.update(
        {
            "linkedin": LinkedInWrapper,
            "indeed": IndeedWrapper,
            "vision_gsi": VisionGSIWrapper,
            "uslandrecords": USLandRecordsWrapper,
        }
    )
    return _WRAPPER_REGISTRY


def register_wrapper(site_prefix: str, wrapper_cls: type[BaseSiteWrapper]) -> None:
    """Register or override a wrapper class for a site prefix."""
    _WRAPPER_REGISTRY[site_prefix] = wrapper_cls


def resolve_wrapper(site_id: str) -> type[BaseSiteWrapper]:
    """Resolve a site_id to its specific wrapper class.

    Falls back to ``BaseSiteWrapper`` for unregistered sites, which
    uses the generic profile-driven extraction path.
    """
    registry = _build_registry()
    if site_id in registry:
        return registry[site_id]

    # Longest-prefix wins for namespaced site ids like "vision_gsi_<town>".
    for prefix in sorted(registry.keys(), key=len, reverse=True):
        cls = registry[prefix]
        if site_id.startswith(prefix):
            return cls
    return BaseSiteWrapper


async def scrape_node(
    request: ScrapeRequest,
    profile: SiteProfile,
    *,
    dedup: DedupEngine | None = None,
    content_store: ContentStore | None = None,
    fixture_store: FixtureStore | None = None,
    wrapper_cls: type[BaseSiteWrapper] | None = None,
) -> ScrapeResult:
    """Execute a scrape run for a single site.

    Tries HTTP first (if ``profile.requires_browser`` is False), then falls
    back to a full browser session.

    Args:
        request: What to scrape and how many records.
        profile: The target site's full configuration.
        dedup: Optional dedup engine for cross-run deduplication.
        content_store: Optional store for persisting extracted records.
        fixture_store: Optional fixture store for runtime drift detection.
        wrapper_cls: Optional custom wrapper class (default: BaseSiteWrapper).
    """
    t_start = time.monotonic()

    ref_fingerprint = None
    if fixture_store:
        ref_fingerprint = fixture_store.load_fingerprint(profile.site_id)

    if not profile.requires_browser:
        http_result = await _try_http_extraction(request, profile)
        if http_result is not None:
            if dedup and http_result.records:
                http_result = await _dedup_result(http_result, profile, dedup)
            if content_store and http_result.records:
                _persist_records(http_result, profile, content_store)
            http_result.timing.total_ms = (time.monotonic() - t_start) * 1000
            return http_result

    effective_cls = wrapper_cls or resolve_wrapper(profile.site_id)
    result = await _browser_extraction(
        request,
        profile,
        dedup=dedup,
        ref_fingerprint=ref_fingerprint,
        wrapper_cls=effective_cls,
    )

    if content_store and result.records:
        _persist_records(result, profile, content_store)

    result.timing.total_ms = (time.monotonic() - t_start) * 1000

    logger.info(
        "scrape_node.complete",
        run_id=request.run_id,
        site_id=profile.site_id,
        records=len(result.records),
        pages=result.pages_visited,
        errors=len(result.errors),
        drift=result.drift_detected,
        total_ms=round(result.timing.total_ms, 1),
    )
    return result


async def _try_http_extraction(
    request: ScrapeRequest,
    profile: SiteProfile,
) -> ScrapeResult | None:
    """Attempt extraction via HTTP only. Returns None to signal escalation."""
    client = StealthHttpClient()
    try:
        search_url = profile.navigation.search_url_template.format(
            **{**request.search_params, "page": 1}
        )
    except KeyError as exc:
        logger.warning(
            "scrape_node.http_escalation_missing_param",
            site_id=profile.site_id,
            missing_param=str(exc),
        )
        return None

    t0 = time.monotonic()
    http_resp = await client.get(search_url, site_id=profile.site_id)
    search_ms = (time.monotonic() - t0) * 1000

    if http_resp.needs_escalation:
        logger.info(
            "scrape_node.http_escalation",
            site_id=profile.site_id,
            reason=http_resp.escalation_reason,
        )
        return None

    if not http_resp.success:
        return ScrapeResult(
            run_id=request.run_id,
            site_id=profile.site_id,
            errors=[
                ScrapeError(
                    classification=ErrorClassification.NETWORK,
                    message=http_resp.error,
                    stage="http_search",
                )
            ],
            timing=TimingBreakdown(search_ms=search_ms),
        )

    from pipelines.scraper.wrappers.base import BaseSiteWrapper

    t0 = time.monotonic()
    wrapper = BaseSiteWrapper.__new__(BaseSiteWrapper)
    wrapper._profile = profile
    wrapper._errors = []
    wrapper._warnings = []
    records = wrapper._extract_from_html(http_resp.html)
    extract_ms = (time.monotonic() - t0) * 1000

    return ScrapeResult(
        run_id=request.run_id,
        site_id=profile.site_id,
        records=records[: request.max_records],
        timing=TimingBreakdown(search_ms=search_ms, extract_ms=extract_ms),
        pages_visited=1,
        errors=wrapper._errors,
        warnings=wrapper._warnings,
    )


async def _browser_extraction(
    request: ScrapeRequest,
    profile: SiteProfile,
    *,
    dedup: DedupEngine | None = None,
    ref_fingerprint: Any = None,
    wrapper_cls: type[BaseSiteWrapper] | None = None,
) -> ScrapeResult:
    """Full browser-based scrape using BrowserManager + BrowserActions."""
    wrapper_class = wrapper_cls or BaseSiteWrapper

    try:
        async with BrowserManager() as browser:
            page = await browser.new_page()
            actions = BrowserActions(page, HumanBehavior())

            wrapper = wrapper_class(
                profile,
                actions,
                dedup=dedup,
                reference_fingerprint=ref_fingerprint,
            )

            result = await wrapper.scrape(
                request.search_params,
                max_records=request.max_records,
                max_pages=request.max_pages,
                run_id=request.run_id,
            )
            return result

    except Exception as exc:
        logger.exception("scrape_node.browser_error", site_id=profile.site_id, error=str(exc)[:300])
        return ScrapeResult(
            run_id=request.run_id,
            site_id=profile.site_id,
            errors=[
                ScrapeError(
                    classification=ErrorClassification.UNKNOWN,
                    message=str(exc)[:500],
                    stage="browser",
                    recoverable=False,
                )
            ],
        )


async def _dedup_result(
    result: ScrapeResult,
    profile: SiteProfile,
    dedup: DedupEngine,
) -> ScrapeResult:
    """Apply dedup to an existing result, mutating dedup_stats."""
    contract = profile.output_contract
    keys = [compute_dedup_key(rec, contract.dedup_fields) for rec in result.records]

    new_keys = await dedup.filter_new(profile.site_id, keys)
    new_key_set = set(new_keys)

    deduped = [rec for rec, key in zip(result.records, keys, strict=True) if key in new_key_set]
    await dedup.add_batch(profile.site_id, new_keys)

    result.dedup_stats.total_extracted = len(result.records)
    result.dedup_stats.new_records = len(deduped)
    result.dedup_stats.duplicates_skipped = len(result.records) - len(deduped)
    result.dedup_stats.bloom_checks = len(keys)
    result.records = deduped
    return result


def _persist_records(
    result: ScrapeResult,
    profile: SiteProfile,
    store: ContentStore,
) -> None:
    """Write extracted records to the content store."""
    contract = profile.output_contract
    keys = [compute_dedup_key(rec, contract.dedup_fields) for rec in result.records]
    store.append_with_metadata(profile.site_id, result.records, keys)
