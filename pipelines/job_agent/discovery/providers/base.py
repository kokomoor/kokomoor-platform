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

    from pipelines.job_agent.discovery.captcha import CaptchaHandler
    from pipelines.job_agent.discovery.human_behavior import HumanBehavior
    from pipelines.job_agent.discovery.models import DiscoveryConfig, ListingRef
    from pipelines.job_agent.discovery.rate_limiter import DomainRateLimiter
    from pipelines.job_agent.models import JobSource, SearchCriteria

logger = structlog.get_logger(__name__)


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
    ) -> list[ListingRef]:
        """Navigate to a single search URL and paginate through results."""
        try:
            await rate_limiter.wait()
            await page.goto(start_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            logger.warning("navigation_failed", url=start_url, exc_info=True)
            return []

        captcha = await captcha_handler.detect(page)
        if captcha.detected:
            outcome = await captcha_handler.handle(
                page,
                captcha,
                strategy=config.captcha_strategy,
                api_key=config.captcha_api_key.get_secret_value(),
            )
            if not outcome.resolved:
                logger.warning("captcha_blocked", url=start_url)
                return []

        await behavior.between_actions_pause(min_s=1.0, max_s=3.0)
        await behavior.simulate_interest_in_page(page)

        page_refs = await self._extract_refs_from_page(page)
        refs = list(page_refs)
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

            # Delay BEFORE the next navigation-triggering interaction.
            await rate_limiter.wait()
            await behavior.between_pages_pause(self.source)
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
