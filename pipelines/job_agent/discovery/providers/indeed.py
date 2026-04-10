"""Indeed job board scraper.

No authentication required. Indeed shows salary data on cards for many
listings, which helps the prefilter work before we fetch full descriptions.

Anti-detection notes for Indeed:
- Indeed has aggressive bot detection that watches for rapid sequential
  page loads, missing referrer headers, and zero mouse movement.
- DomainRateLimiter enforces the per-page delays (5-14s).
- simulate_interest_in_page() scrolls naturally before extracting links.
- We use the standard search URL with filters applied via query params;
  we do NOT use Indeed's internal API endpoints (they fingerprint requests).
- The User-Agent and Accept-Language headers on the BrowserManager context
  are already set to realistic values via apply_stealth_defaults().
- Do not click into individual job postings during discovery -- card data only.
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
    from pipelines.job_agent.discovery.human_behavior import HumanBehavior
    from pipelines.job_agent.discovery.models import DiscoveryConfig, ListingRef
    from pipelines.job_agent.discovery.rate_limiter import DomainRateLimiter
    from pipelines.job_agent.models import SearchCriteria

logger = structlog.get_logger(__name__)

_MAX_URL_COMBOS = 3
_MAX_KEYWORDS_PER_QUERY = 4
_INDEED_FULLTIME_FILTER = "0kf%3Aattr(DSQF7)%3B"


class IndeedProvider(BaseProvider):
    """Browser-based Indeed search result scraper."""

    source: ClassVar[JobSource] = JobSource.INDEED

    def requires_auth(self) -> bool:
        return False

    def base_domain(self) -> str:
        return "www.indeed.com"

    async def is_authenticated(self, page: Page) -> bool:
        return True

    # ------------------------------------------------------------------
    # URL construction
    # ------------------------------------------------------------------

    def _build_search_urls(self, criteria: SearchCriteria, config: DiscoveryConfig) -> list[str]:
        urls: list[str] = []

        if criteria.locations:
            location = criteria.locations[0]
        elif criteria.remote_ok:
            location = "Remote"
        else:
            location = ""

        if criteria.keywords:
            kw_group = " ".join(criteria.keywords[:_MAX_KEYWORDS_PER_QUERY])
            urls.append(self._make_search_url(kw_group, location))

        for role in criteria.target_roles:
            if len(urls) >= _MAX_URL_COMBOS:
                break
            urls.append(self._make_search_url(role, location))

        if not urls:
            urls.append(self._make_search_url("", location))

        return urls[:_MAX_URL_COMBOS]

    @staticmethod
    def _make_search_url(query: str, location: str) -> str:
        return (
            f"https://www.indeed.com/jobs?"
            f"q={quote_plus(query)}&l={quote_plus(location)}"
            f"&fromage=14&sc={_INDEED_FULLTIME_FILTER}"
        )

    # ------------------------------------------------------------------
    # Card extraction
    # ------------------------------------------------------------------

    async def _extract_refs_from_page(self, page: Page) -> list[ListingRef]:
        from pipelines.job_agent.discovery.models import ListingRef

        cards = await page.query_selector_all("[data-testid='slider_item']")
        if not cards:
            cards = await page.query_selector_all(".job_seen_beacon")

        seen_jks: set[str] = set()
        refs: list[ListingRef] = []

        for card in cards:
            jk = await self._extract_jk(card)
            if not jk or jk in seen_jks:
                continue
            seen_jks.add(jk)

            title = await self._safe_text(await card.query_selector("[data-testid='jobTitle']"))
            if not title:
                title = await self._safe_text(await card.query_selector("h2.jobTitle a"))

            company = await self._safe_text(
                await card.query_selector("[data-testid='company-name']")
            )
            location = await self._safe_text(
                await card.query_selector("[data-testid='text-location']")
            )

            salary_el = await card.query_selector("[data-testid='attribute_snippet_testid']")
            if salary_el is None:
                salary_el = await card.query_selector(".salary-snippet-container")
            salary_text = await self._safe_text(salary_el)

            canonical = canonicalize_url(f"https://www.indeed.com/viewjob?jk={jk}")
            refs.append(
                ListingRef(
                    url=canonical,
                    title=title,
                    company=company,
                    source=JobSource.INDEED,
                    location=location,
                    salary_text=salary_text,
                )
            )

        logger.debug("indeed_extract", count=len(refs))
        return refs

    @staticmethod
    async def _extract_jk(card: ElementHandle) -> str:
        """Pull the job key from a card element, trying link then card attrs."""
        try:
            link = await card.query_selector("a[data-jk]")
            if link:
                jk = await link.get_attribute("data-jk")
                if jk:
                    return jk
            jk = await card.get_attribute("data-jk")
            return jk or ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Pagination (dual-selector next button)
    # ------------------------------------------------------------------

    def _next_page_selector(self) -> str | None:
        return "[data-testid='pagination-page-next']"

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
                next_btn = await page.query_selector("[data-testid='pagination-page-next']")
                if not next_btn:
                    next_btn = await page.query_selector("a[aria-label='Next Page']")
            except Exception:
                break
            if not next_btn or not await next_btn.is_visible():
                break

            await behavior.human_click(page, next_btn)
            await rate_limiter.wait()
            await behavior.between_pages_pause(self.source)

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
