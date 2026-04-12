"""BaseProvider -- shared browser lifecycle for all browser-based adapters.

Subclasses implement three abstract methods:
  _build_search_urls(criteria, config) -> list[str]
    Return a list of search result page URLs (one per keyword/location combo).
  _extract_refs_from_page(page) -> list[ListingRef]
    Parse the current search results page and return ListingRef objects.
    Must canonicalize all URLs via url_utils.canonicalize_url().
  _next_page_selector() -> str | None
    CSS selector for the "next page" button, or None if provider uses infinite scroll.

BaseProvider.run_search() drives the full pagination loop using these three methods.
Subclasses must NOT override run_search() unless they have a genuinely different
pagination model (e.g. infinite scroll -- in that case, override and call
_extract_refs_from_page for each loaded batch).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from typing import ClassVar

    from playwright.async_api import ElementHandle, Page

    from core.browser.captcha import CaptchaHandler
    from core.browser.debug_capture import FailureCapture
    from core.browser.human_behavior import HumanBehavior
    from pipelines.job_agent.discovery.models import DiscoveryConfig, ListingRef
    from pipelines.job_agent.discovery.rate_limiter import DomainRateLimiter
    from pipelines.job_agent.models import JobSource, SearchCriteria

logger = structlog.get_logger(__name__)

_NAV_RETRY_LIMIT = 1
_ERROR_TITLE_SIGNALS = frozenset(
    {
        "page not found",
        "error",
        "access denied",
        "403 forbidden",
        "502 bad gateway",
        "503 service",
        "blocked",
    }
)


class BaseProvider(ABC):
    """Abstract base for browser-based provider adapters.

    Provides the pagination loop (``run_search``) and safe helpers.
    Subclasses supply URL construction, page extraction, and next-page
    selectors.
    """

    source: ClassVar[JobSource]

    def requires_auth(self) -> bool:
        return False

    @abstractmethod
    def base_domain(self) -> str: ...

    @abstractmethod
    async def is_authenticated(self, page: Page) -> bool: ...

    async def authenticate(
        self,
        page: Page,
        *,
        email: str,
        password: str,
        behavior: HumanBehavior,
    ) -> bool:
        return True

    @abstractmethod
    def _build_search_urls(
        self, criteria: SearchCriteria, config: DiscoveryConfig
    ) -> list[str]: ...

    @abstractmethod
    async def _extract_refs_from_page(self, page: Page) -> list[ListingRef]: ...

    def _next_page_selector(self) -> str | None:
        return None

    # ------------------------------------------------------------------
    # Navigation guard
    # ------------------------------------------------------------------

    async def _safe_navigate(
        self,
        page: Page,
        url: str,
        *,
        rate_limiter: DomainRateLimiter,
        captcha_handler: CaptchaHandler,
        config: DiscoveryConfig,
        capture: FailureCapture | None = None,
    ) -> bool:
        """Navigate to a URL with retry, validation, and captcha detection.

        Returns True if the page is ready for extraction.
        """
        for attempt in range(_NAV_RETRY_LIMIT + 1):
            try:
                await rate_limiter.wait()
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except Exception:
                logger.warning(
                    "navigation_failed",
                    url=url,
                    attempt=attempt + 1,
                    exc_info=True,
                )
                if attempt < _NAV_RETRY_LIMIT:
                    continue
                if capture:
                    await capture.capture_page_failure(
                        source=self.source,
                        stage="navigation_exhausted",
                        reason=f"all_navigation_attempts_failed_for_{url[:80]}",
                        page=page,
                    )
                return False

            if self._is_error_page(page):
                logger.warning(
                    "navigation_error_page",
                    url=url,
                    actual_url=page.url,
                    attempt=attempt + 1,
                )
                if attempt < _NAV_RETRY_LIMIT:
                    continue
                if capture:
                    await capture.capture_page_failure(
                        source=self.source,
                        stage="error_page_detected",
                        reason="navigation_landed_on_error_page",
                        page=page,
                    )
                return False

            captcha = await captcha_handler.detect(page)
            if captcha.detected:
                outcome = await captcha_handler.handle(
                    page,
                    captcha,
                    strategy=config.captcha_strategy,
                    api_key=config.captcha_api_key.get_secret_value(),
                )
                if not outcome.resolved:
                    logger.warning("captcha_blocked", url=url)
                    if capture:
                        await capture.capture_page_failure(
                            source=self.source,
                            stage="captcha_blocked",
                            reason=f"captcha_{captcha.captcha_type}_not_resolved",
                            page=page,
                        )
                    return False

            return True

        return False

    @staticmethod
    def _is_error_page(page: Page) -> bool:
        """Quick heuristic: did we land on an error or block page?"""
        url_lower = page.url.lower()
        return any(
            sig in url_lower for sig in ("/error", "/404", "/403", "/blocked", "/unavailable")
        )

    # ------------------------------------------------------------------
    # Pagination loop
    # ------------------------------------------------------------------

    async def run_search(
        self,
        page: Page,
        criteria: SearchCriteria,
        config: DiscoveryConfig,
        *,
        behavior: HumanBehavior,
        rate_limiter: DomainRateLimiter,
        captcha_handler: CaptchaHandler,
        capture: FailureCapture | None = None,
    ) -> list[ListingRef]:
        """Drive search across all keyword/location URLs with pagination."""
        refs: list[ListingRef] = []
        search_urls = self._build_search_urls(criteria, config)

        for search_url in search_urls:
            page_refs = await self._run_single_search(
                page,
                search_url,
                config,
                behavior=behavior,
                rate_limiter=rate_limiter,
                captcha_handler=captcha_handler,
                capture=capture,
            )
            refs.extend(page_refs)
            if len(refs) >= config.max_listings_per_provider:
                break

        return refs[: config.max_listings_per_provider]

    async def _run_single_search(
        self,
        page: Page,
        start_url: str,
        config: DiscoveryConfig,
        *,
        behavior: HumanBehavior,
        rate_limiter: DomainRateLimiter,
        captcha_handler: CaptchaHandler,
        capture: FailureCapture | None = None,
    ) -> list[ListingRef]:
        """Navigate to a single search URL and paginate through results."""
        ready = await self._safe_navigate(
            page,
            start_url,
            rate_limiter=rate_limiter,
            captcha_handler=captcha_handler,
            config=config,
            capture=capture,
        )
        if not ready:
            return []

        await behavior.between_actions_pause(min_s=1.0, max_s=3.0)
        await behavior.simulate_interest_in_page(page)

        page_refs = await self._extract_refs_from_page(page)
        refs = list(page_refs)

        if not refs and capture:
            await capture.capture_page_failure(
                source=self.source,
                stage="zero_results_first_page",
                reason="extraction_returned_zero_refs_on_first_page",
                page=page,
                extra={"url": start_url},
            )

        page_count = 1

        for _ in range(config.max_pages_per_search - 1):
            next_sel = self._next_page_selector()
            if next_sel is None:
                break
            try:
                next_btn = await page.query_selector(next_sel)
            except Exception:
                break
            if not next_btn or not await next_btn.is_visible():
                break

            await rate_limiter.wait()
            await behavior.between_navigations_pause()
            await behavior.human_click(page, next_btn)

            captcha = await captcha_handler.detect(page)
            if captcha.detected:
                await captcha_handler.handle(
                    page,
                    captcha,
                    strategy=config.captcha_strategy,
                    api_key=config.captcha_api_key.get_secret_value(),
                )
                break

            await behavior.simulate_interest_in_page(page)
            page_refs = await self._extract_refs_from_page(page)
            refs.extend(page_refs)
            page_count += 1
            if len(refs) >= config.max_listings_per_provider:
                break

        logger.info(
            "search_complete",
            url=start_url,
            pages=page_count,
            refs=len(refs),
        )
        return refs

    # ------------------------------------------------------------------
    # Helpers for subclasses
    # ------------------------------------------------------------------

    @staticmethod
    async def _safe_text(element: ElementHandle | None) -> str:
        """Extract text content from an element, returning '' on failure."""
        if element is None:
            return ""
        try:
            text = await element.text_content()
            return (text or "").strip()
        except Exception:
            return ""
