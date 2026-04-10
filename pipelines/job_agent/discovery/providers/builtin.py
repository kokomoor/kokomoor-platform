"""Built In job board scraper (builtin.com and city editions).

Built In aggregates tech/startup jobs. City editions (builtinboston.com,
builtinnyc.com, etc.) are better for local searches. Uses the main site
for remote/national searches.

No authentication required. Built In has moderate bot detection -- primarily
rate-based. DomainRateLimiter (3-8s delays) is sufficient.

Anti-detection notes:
- Built In's bot detection watches for referrer header absence. Our browser
  context sends Referer automatically for in-site navigation.
- Do not use the Built In internal API endpoints (they fingerprint by token).
- Card structure has changed frequently -- use multiple fallback selectors.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote_plus

import structlog

from pipelines.job_agent.discovery.providers.base import BaseProvider
from pipelines.job_agent.discovery.url_utils import canonicalize_url
from pipelines.job_agent.models import JobSource

if TYPE_CHECKING:
    from typing import ClassVar

    from playwright.async_api import ElementHandle, Page

    from pipelines.job_agent.discovery.captcha import CaptchaHandler
    from pipelines.job_agent.discovery.debug_capture import FailureCapture
    from pipelines.job_agent.discovery.human_behavior import HumanBehavior
    from pipelines.job_agent.discovery.models import DiscoveryConfig, ListingRef
    from pipelines.job_agent.discovery.rate_limiter import DomainRateLimiter
    from pipelines.job_agent.models import SearchCriteria

logger = structlog.get_logger(__name__)

_MAX_URL_COMBOS = 3

_CITY_DOMAINS: dict[str, str] = {
    "boston": "builtinboston.com",
    "new york": "builtinnyc.com",
    "nyc": "builtinnyc.com",
    "san francisco": "builtinsf.com",
    "sf": "builtinsf.com",
    "bay area": "builtinsf.com",
    "seattle": "builtinseattle.com",
    "austin": "builtinaustin.com",
    "chicago": "builtinchicago.com",
    "los angeles": "builtinla.com",
    "la": "builtinla.com",
    "denver": "builtincolorado.com",
    "boulder": "builtincolorado.com",
}


class BuiltInProvider(BaseProvider):
    """Browser-based Built In search result scraper."""

    source: ClassVar[JobSource] = JobSource.BUILTIN

    def requires_auth(self) -> bool:
        return False

    def base_domain(self) -> str:
        return "builtin.com"

    async def is_authenticated(self, page: Page) -> bool:
        return True

    # ------------------------------------------------------------------
    # URL construction
    # ------------------------------------------------------------------

    @staticmethod
    def _choose_base_url(locations: list[str]) -> str:
        """Map location strings to city-specific Built In domains."""
        for loc in locations:
            loc_lower = loc.lower()
            for key, domain in _CITY_DOMAINS.items():
                if key in loc_lower:
                    return domain
        return "builtin.com"

    def _build_search_urls(self, criteria: SearchCriteria, config: DiscoveryConfig) -> list[str]:
        base = self._choose_base_url(criteria.locations)
        remote = "1" if criteria.remote_ok else "0"
        urls: list[str] = []

        if criteria.target_roles:
            for role in criteria.target_roles:
                if len(urls) >= _MAX_URL_COMBOS:
                    break
                urls.append(f"https://{base}/jobs?search={quote_plus(role)}&remote={remote}")
        elif criteria.keywords:
            query = " ".join(criteria.keywords[:4])
            urls.append(f"https://{base}/jobs?search={quote_plus(query)}&remote={remote}")
        else:
            urls.append(f"https://{base}/jobs?remote={remote}")

        return urls[:_MAX_URL_COMBOS]

    # ------------------------------------------------------------------
    # Card extraction
    # ------------------------------------------------------------------

    async def _extract_refs_from_page(self, page: Page) -> list[ListingRef]:
        from pipelines.job_agent.discovery.models import ListingRef

        cards = await page.query_selector_all("[data-id='job-card']")
        if not cards:
            cards = await page.query_selector_all(".job-card")
        if not cards:
            cards = await page.query_selector_all("li[data-test='job-list-item']")

        refs: list[ListingRef] = []
        seen_urls: set[str] = set()

        for card in cards:
            title = await self._safe_text(await card.query_selector("[data-test='job-title']"))
            if not title:
                title = await self._safe_text(await card.query_selector("h2"))
            if not title:
                title = await self._safe_text(await card.query_selector("h3"))

            company = await self._safe_text(
                await card.query_selector("[data-test='company-title']")
            )
            if not company:
                company = await self._safe_text(
                    await card.query_selector("[data-test='company-name']")
                )

            location = await self._safe_text(
                await card.query_selector("[data-test='job-location']")
            )
            if not location:
                location = await self._safe_text(await card.query_selector(".job-location"))

            href = await self._extract_card_href(card)
            if not href:
                continue

            if not href.startswith("http"):
                base = self._choose_base_url([location]) if location else "builtin.com"
                href = f"https://{base}{href}"

            url = canonicalize_url(href)
            if url in seen_urls:
                continue
            seen_urls.add(url)

            refs.append(
                ListingRef(
                    url=url,
                    title=title,
                    company=company,
                    source=JobSource.BUILTIN,
                    location=location,
                )
            )

        logger.debug("builtin_extract", count=len(refs))
        return refs

    @staticmethod
    async def _extract_card_href(card: ElementHandle) -> str:
        """Pull the job URL from a card, trying multiple selector strategies."""
        try:
            link = await card.query_selector("a[data-test='job-title-link']")
            if link:
                href = await link.get_attribute("href")
                if href:
                    return href

            for link_el in await card.query_selector_all("a"):
                href = await link_el.get_attribute("href")
                if href and "/jobs/" in href:
                    return href
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------
    # Pagination (dual-selector next button)
    # ------------------------------------------------------------------

    def _next_page_selector(self) -> str | None:
        # Primary selector; fallback handled in _run_single_search override
        return "[aria-label='Next page']"

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
        """Navigate and paginate with dual-selector next-button resilience."""
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
            try:
                next_btn = await page.query_selector("[aria-label='Next page']")
                if not next_btn:
                    next_btn = await page.query_selector("[data-test='pagination-next']")
            except Exception:
                break
            if not next_btn or not await next_btn.is_visible():
                break

            # Delay BEFORE pagination interaction to avoid instant next request.
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
