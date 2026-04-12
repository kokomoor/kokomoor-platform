"""Generic profile-driven site wrapper.

``BaseSiteWrapper`` implements the full scrape lifecycle (auth, search,
extract, paginate, detail) driven entirely by a ``SiteProfile``.  Most
sites can be scraped with only a profile — override individual methods
for site-specific quirks (ASP.NET postbacks, JavaScript pagination, etc.).

Design rules:
- Every public method returns structured data (never raises on routine failures).
- All browser interaction goes through ``BrowserActions`` for uniform stealth.
- Rate limiting is applied between every navigation.
- Extraction errors are collected, not raised, so a partial scrape still yields data.
"""

from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING, Any

import structlog
from bs4 import BeautifulSoup

from core.browser.human_behavior import HumanBehavior
from core.browser.rate_limiter import RateLimiter, RateLimitProfile
from core.scraper.dedup import compute_dedup_key
from core.scraper.fixtures import compute_fingerprint
from pipelines.scraper.models import (
    DedupStats,
    ErrorClassification,
    PaginationStrategy,
    RateLimitConfig,
    ScrapeError,
    ScrapeResult,
    SiteProfile,
    TimingBreakdown,
)

if TYPE_CHECKING:
    from core.browser.actions import BrowserActions, NavigationResult
    from core.scraper.dedup import DedupEngine
    from core.scraper.fixtures import StructuralFingerprint

logger = structlog.get_logger(__name__)


def _profile_to_rate_limit(cfg: RateLimitConfig) -> RateLimitProfile:
    return RateLimitProfile(
        min_delay_s=cfg.min_delay_s,
        max_delay_s=cfg.max_delay_s,
        pages_before_long_pause=cfg.pages_before_long_pause,
        long_pause_min_s=cfg.long_pause_min_s,
        long_pause_max_s=cfg.long_pause_max_s,
    )


class BaseSiteWrapper:
    """Profile-driven scraper that implements the full lifecycle.

    Subclasses may override any of the ``_do_*`` methods for site-specific
    behavior.  The public ``scrape()`` method orchestrates the full flow.
    """

    def __init__(
        self,
        profile: SiteProfile,
        actions: BrowserActions,
        *,
        dedup: DedupEngine | None = None,
        reference_fingerprint: StructuralFingerprint | None = None,
    ) -> None:
        self._profile = profile
        self._actions = actions
        self._dedup = dedup
        self._ref_fingerprint = reference_fingerprint
        self._rate_limiter = RateLimiter(
            profile.site_id, _profile_to_rate_limit(profile.rate_limit)
        )
        self._behavior = HumanBehavior()
        self._errors: list[ScrapeError] = []
        self._warnings: list[str] = []

    @property
    def profile(self) -> SiteProfile:
        return self._profile

    @property
    def site_id(self) -> str:
        return self._profile.site_id

    # ------------------------------------------------------------------
    # Public scrape orchestrator
    # ------------------------------------------------------------------

    async def scrape(
        self,
        search_params: dict[str, Any],
        *,
        max_records: int = 500,
        max_pages: int | None = None,
        run_id: str = "",
    ) -> ScrapeResult:
        """Execute the full scrape lifecycle.

        1. Authenticate (if required)
        2. Navigate to search results
        3. Extract records from each page
        4. Paginate until done
        5. Deduplicate
        6. Return structured result
        """
        t_start = time.monotonic()
        timing = TimingBreakdown()
        all_records: list[dict[str, Any]] = []
        pages_visited = 0
        effective_max_pages = max_pages or self._profile.max_pages_per_search
        empty_consecutive = 0
        unchanged_pages = 0
        last_signature = ""

        t0 = time.monotonic()
        auth_ok = await self._do_authenticate()
        timing.auth_ms = (time.monotonic() - t0) * 1000
        if not auth_ok:
            return ScrapeResult(
                run_id=run_id,
                site_id=self.site_id,
                errors=self._errors,
                timing=timing,
            )

        t0 = time.monotonic()
        search_url = self._build_search_url(search_params, page=1)
        nav = await self._do_navigate(search_url)
        timing.search_ms = (time.monotonic() - t0) * 1000

        if not nav.success:
            self._errors.append(
                ScrapeError(
                    classification=ErrorClassification.NETWORK,
                    message=f"Navigation failed: {nav.error}",
                    stage="search",
                )
            )
            return ScrapeResult(
                run_id=run_id,
                site_id=self.site_id,
                errors=self._errors,
                timing=timing,
            )

        drift_detected = False
        fingerprint_similarity: float | None = None

        while pages_visited < effective_max_pages:
            pages_visited += 1

            if self._ref_fingerprint:
                page_html = await self._actions.page.content()
                current_fp = compute_fingerprint(page_html)
                from core.scraper.fixtures import compare_fingerprints

                drift = compare_fingerprints(
                    self._ref_fingerprint,
                    current_fp,
                    threshold=self._profile.drift_threshold,
                )
                fingerprint_similarity = drift.similarity
                if drift.drifted:
                    drift_detected = True
                    self._warnings.append(
                        f"Structural drift detected (similarity={drift.similarity:.2f})"
                    )
                    logger.warning(
                        "wrapper.drift_detected",
                        site_id=self.site_id,
                        similarity=drift.similarity,
                        severity=drift.severity,
                    )

            t0 = time.monotonic()
            page_records = await self._do_extract_page()
            timing.extract_ms += (time.monotonic() - t0) * 1000

            if not page_records:
                empty_consecutive += 1
                if empty_consecutive >= self._profile.output_contract.max_empty_pages_before_stop:
                    logger.info(
                        "wrapper.empty_pages_stop",
                        site_id=self.site_id,
                        consecutive_empty=empty_consecutive,
                    )
                    break
            else:
                empty_consecutive = 0
                signature = "|".join(
                    sorted(
                        str(compute_dedup_key(rec, self._profile.output_contract.dedup_fields))
                        for rec in page_records
                    )
                )
                if signature and signature == last_signature:
                    unchanged_pages += 1
                    if unchanged_pages >= 2:
                        logger.info(
                            "wrapper.stale_pagination_stop",
                            site_id=self.site_id,
                            page=pages_visited,
                        )
                        break
                else:
                    unchanged_pages = 0
                last_signature = signature
                all_records.extend(page_records)

            if len(all_records) >= max_records:
                all_records = all_records[:max_records]
                break

            t0 = time.monotonic()
            has_next = await self._do_paginate(pages_visited)
            timing.paginate_ms += (time.monotonic() - t0) * 1000

            if not has_next:
                break

            await self._rate_limiter.wait()
            await self._behavior.between_navigations_pause()

        t0 = time.monotonic()
        dedup_stats = DedupStats(total_extracted=len(all_records))
        if self._dedup and all_records:
            contract = self._profile.output_contract
            keys = [compute_dedup_key(rec, contract.dedup_fields) for rec in all_records]
            dedup_stats.bloom_checks = len(keys)
            new_keys = await self._dedup.filter_new(self.site_id, keys)
            new_key_set = set(new_keys)

            deduped: list[dict[str, Any]] = []
            for rec, key in zip(all_records, keys, strict=True):
                if key in new_key_set:
                    deduped.append(rec)
            dedup_stats.new_records = len(deduped)
            dedup_stats.duplicates_skipped = len(all_records) - len(deduped)
            all_records = deduped

            await self._dedup.add_batch(self.site_id, new_keys)
        else:
            dedup_stats.new_records = len(all_records)

        timing.dedup_ms = (time.monotonic() - t0) * 1000
        timing.total_ms = (time.monotonic() - t_start) * 1000

        logger.info(
            "wrapper.scrape_complete",
            site_id=self.site_id,
            records=len(all_records),
            pages=pages_visited,
            errors=len(self._errors),
            drift=drift_detected,
            total_ms=round(timing.total_ms, 1),
        )

        return ScrapeResult(
            run_id=run_id,
            site_id=self.site_id,
            records=all_records,
            dedup_stats=dedup_stats,
            timing=timing,
            errors=self._errors,
            warnings=self._warnings,
            pages_visited=pages_visited,
            drift_detected=drift_detected,
            fingerprint_similarity=fingerprint_similarity,
        )

    # ------------------------------------------------------------------
    # Lifecycle methods (override for site-specific behavior)
    # ------------------------------------------------------------------

    async def _do_authenticate(self) -> bool:
        """Authenticate if the profile requires it. Returns True on success."""
        auth = self._profile.auth
        if auth.type.value == "none":
            return True

        if auth.type.value == "credential_form":
            return await self._credential_form_auth(auth)

        self._warnings.append(f"Unsupported auth type: {auth.type}")
        return True

    async def _credential_form_auth(self, auth: Any) -> bool:
        """Handle username/password form-based auth."""
        import os

        username = os.environ.get(f"KP_{auth.env_username_key}", "")
        password = os.environ.get(f"KP_{auth.env_password_key}", "")
        if not username or not password:
            self._errors.append(
                ScrapeError(
                    classification=ErrorClassification.AUTH,
                    message=f"Missing credentials: KP_{auth.env_username_key}",
                    stage="auth",
                    recoverable=False,
                )
            )
            return False

        if auth.login_url:
            nav = await self._actions.goto(auth.login_url)
            if not nav.success:
                self._errors.append(
                    ScrapeError(
                        classification=ErrorClassification.AUTH,
                        message=f"Cannot reach login page: {nav.error}",
                        stage="auth",
                    )
                )
                return False

        fill_result = await self._actions.fill(auth.username_selector, username)
        if not fill_result.success:
            self._errors.append(
                ScrapeError(
                    classification=ErrorClassification.SELECTOR,
                    message=f"Username field not found: {auth.username_selector}",
                    stage="auth",
                )
            )
            return False

        fill_result = await self._actions.fill(auth.password_selector, password)
        if not fill_result.success:
            self._errors.append(
                ScrapeError(
                    classification=ErrorClassification.SELECTOR,
                    message=f"Password field not found: {auth.password_selector}",
                    stage="auth",
                )
            )
            return False

        click_result = await self._actions.click(auth.submit_selector)
        if not click_result.success:
            self._errors.append(
                ScrapeError(
                    classification=ErrorClassification.SELECTOR,
                    message=f"Submit button not found: {auth.submit_selector}",
                    stage="auth",
                )
            )
            return False

        if auth.success_indicator:
            found = await self._actions.wait_for(auth.success_indicator, timeout_ms=10_000)
            if not found:
                self._errors.append(
                    ScrapeError(
                        classification=ErrorClassification.AUTH,
                        message="Auth success indicator not found after login",
                        stage="auth",
                    )
                )
                return False

        logger.info("wrapper.auth_success", site_id=self.site_id)
        return True

    async def _do_navigate(self, url: str) -> NavigationResult:
        """Navigate to a URL via BrowserActions."""
        return await self._actions.goto(url)

    async def _do_extract_page(self) -> list[dict[str, Any]]:
        """Extract records from the current page using profile selectors."""
        html = await self._actions.page.content()
        return self._extract_from_html(html)

    def _extract_from_html(self, html: str) -> list[dict[str, Any]]:
        """Parse records from raw HTML using the selector config."""
        selectors = self._profile.selectors
        soup = BeautifulSoup(html, "html.parser")
        items = soup.select(selectors.result_item)

        records: list[dict[str, Any]] = []
        for item in items:
            record: dict[str, Any] = {}
            for field_name, selector in selectors.field_map.items():
                try:
                    el = item.select_one(selector)
                    if el:
                        if el.get("href"):
                            record[field_name] = self._normalize_url(str(el["href"]))
                        else:
                            record[field_name] = el.get_text(strip=True)
                    else:
                        record[field_name] = ""
                except Exception as exc:
                    record[field_name] = ""
                    self._errors.append(
                        ScrapeError(
                            classification=ErrorClassification.EXTRACTION,
                            message=f"Field '{field_name}' extraction failed: {str(exc)[:200]}",
                            stage="extract",
                        )
                    )
            if any(v for v in record.values()):
                records.append(record)

        logger.debug(
            "wrapper.extracted",
            site_id=self.site_id,
            items_found=len(items),
            records=len(records),
        )
        return records

    async def _do_paginate(self, current_page: int) -> bool:
        """Navigate to the next page. Returns True if successful."""
        nav = self._profile.navigation
        strategy = nav.pagination

        if strategy == PaginationStrategy.NONE:
            return False

        if strategy == PaginationStrategy.NEXT_BUTTON:
            if not nav.next_button_selector:
                return False
            result = await self._actions.click(nav.next_button_selector)
            if not result.success:
                return False
            await self._actions.wait_for(self._profile.selectors.result_item, timeout_ms=10_000)
            return True

        if strategy == PaginationStrategy.URL_PARAMETER:
            next_url = self._build_search_url({}, page=current_page + 1)
            if "{" in next_url and "}" in next_url:
                self._warnings.append("Cannot paginate: unresolved URL template placeholders")
                return False
            nav_result = await self._actions.goto(next_url)
            return nav_result.success

        if strategy == PaginationStrategy.INFINITE_SCROLL:
            import asyncio

            before_height: int = await self._actions.page.evaluate("document.body.scrollHeight")
            await self._actions.scroll("down", amount=1500)
            await asyncio.sleep(random.uniform(1.3, 2.7))
            after_height: int = await self._actions.page.evaluate("document.body.scrollHeight")
            return bool(after_height > before_height)

        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_search_url(self, params: dict[str, Any], *, page: int = 1) -> str:
        template = self._profile.navigation.search_url_template
        merged = {**params, "page": page}
        try:
            return template.format(**merged)
        except KeyError:
            return template

    def _normalize_url(self, url: str) -> str:
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return self._profile.base_url.rstrip("/") + url
        return url

    # ------------------------------------------------------------------
    # HTML-only extraction (for offline fixture testing)
    # ------------------------------------------------------------------

    def extract_from_fixture(self, html: str) -> list[dict[str, Any]]:
        """Run extraction logic against raw HTML (no browser needed).

        This is the primary method called by offline fixture tests.
        """
        return self._extract_from_html(html)
