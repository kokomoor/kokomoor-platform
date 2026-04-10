"""LinkedIn job search scraper -- the most anti-detection-sensitive provider.

Design philosophy:
- Session persistence is MANDATORY. A fresh session without cookies will
  hit CAPTCHA within 1-2 page loads. The session must be warmed up
  (used multiple times over multiple days) before it reliably avoids detection.
- We operate as an authenticated user browsing jobs normally -- not as a
  scraper extracting structured data.
- Every action mimics what a human does: we read the results page before
  extracting links, we scroll naturally, we pause between pages.
- We extract data from search result CARDS only. We never navigate into
  individual job detail pages during discovery -- this keeps the session
  footprint small and avoids triggering "job view" rate limits.
- The canonical job URL we store is constructed from the numeric job ID
  extracted from the card -- NOT from the card's href (which contains
  tracking params that LinkedIn uses to identify scrapers).

Session warm-up note for first use:
  When no session exists yet, the first run may encounter a CAPTCHA or
  email verification challenge. The CAPTCHA handler will notify the owner.
  After the first successful authenticated session is saved, subsequent
  runs should be reliable for 48-72 hours before re-authentication is needed.

LinkedIn URL structure:
  Search: https://www.linkedin.com/jobs/search/?keywords={kw}&location={loc}&f_TPR=r604800&f_WT=2
  f_TPR=r604800 = posted in the past week (604800 seconds)
  f_WT=2 = remote only (omit for all locations)
  Card job IDs: data-entity-urn="urn:li:job:{id}" or data-job-id="{id}"
  Canonical job URL: https://www.linkedin.com/jobs/view/{id}/
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

import structlog

from pipelines.job_agent.discovery.providers.base import BaseProvider
from pipelines.job_agent.discovery.url_utils import extract_job_id_from_linkedin_url
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

_MAX_SEARCH_URLS = 6
_MAX_KEYWORDS_PER_GROUP = 3
_PAST_WEEK_FILTER = "r604800"
_URN_RE = re.compile(r"urn:li:job:(\d+)")
_LOCATION_HINTS = frozenset({",", "remote", "hybrid", "united states", "usa"})


class LinkedInProvider(BaseProvider):
    """Browser-based LinkedIn job search scraper.

    Highest anti-detection sensitivity of any provider. Every method is
    designed around making the session indistinguishable from a human
    browsing LinkedIn on a laptop.
    """

    source: ClassVar[JobSource] = JobSource.LINKEDIN

    def requires_auth(self) -> bool:
        return True

    def base_domain(self) -> str:
        return "www.linkedin.com"

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def is_authenticated(self, page: Page) -> bool:
        """Check for a logged-in LinkedIn session via nav avatar selectors."""
        url_lower = page.url.lower()
        if "/login" in url_lower or "/checkpoint" in url_lower:
            return False

        selectors = (
            ".global-nav__me-photo",
            "[data-control-name='identity_welcome_message']",
            "nav[aria-label*='primary'] a[href*='/in/']",
        )
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    return True
            except Exception:
                continue

        try:
            has_feed_identity: bool = await page.evaluate(
                "document.querySelector('.feed-identity-module') !== null"
            )
            if has_feed_identity:
                return True
        except Exception:
            pass

        return False

    async def authenticate(
        self,
        page: Page,
        *,
        email: str,
        password: str,
        behavior: HumanBehavior,
    ) -> bool:
        """Perform the LinkedIn email/password login flow.

        Every step uses HumanBehavior to mimic real user interaction.
        """
        try:
            await page.goto(
                "https://www.linkedin.com",
                wait_until="domcontentloaded",
                timeout=20_000,
            )
        except Exception:
            logger.warning("linkedin_home_nav_failed", exc_info=True)
            return False

        await behavior.reading_pause(800)

        sign_in_btn = None
        for sel in (
            "a[data-tracking-control-name*='login']",
            "a[href*='/login']",
            "a.nav__button-secondary",
        ):
            try:
                sign_in_btn = await page.query_selector(sel)
                if sign_in_btn:
                    logger.debug("linkedin_sign_in_selector", selector=sel)
                    break
            except Exception:
                continue

        if sign_in_btn:
            await behavior.between_actions_pause(min_s=0.5, max_s=1.5)
            await behavior.human_click(page, sign_in_btn)
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        else:
            try:
                await page.goto(
                    "https://www.linkedin.com/login",
                    wait_until="domcontentloaded",
                    timeout=15_000,
                )
            except Exception:
                logger.warning("linkedin_login_nav_failed", exc_info=True)
                return False

        try:
            username_input = await page.wait_for_selector("input#username", timeout=10_000)
        except Exception:
            logger.warning("linkedin_username_field_not_found")
            return False

        if username_input is None:
            return False

        await behavior.human_click(page, username_input)
        await behavior.between_actions_pause(min_s=0.3, max_s=0.8)
        await behavior.type_with_cadence(username_input, email)
        await behavior.between_actions_pause(min_s=0.5, max_s=1.5)

        password_input = await page.query_selector("input#password")
        if password_input is None:
            logger.warning("linkedin_password_field_not_found")
            return False

        await behavior.human_click(page, password_input)
        await behavior.type_with_cadence(password_input, password)
        await behavior.between_actions_pause(min_s=0.4, max_s=1.2)

        submit_btn = await page.query_selector(
            "button[type='submit'], button[aria-label='Sign in']"
        )
        if submit_btn is None:
            logger.warning("linkedin_submit_button_not_found")
            return False

        await behavior.hover_before_click(page, submit_btn)
        await behavior.between_actions_pause(min_s=0.2, max_s=0.5)
        await behavior.human_click(page, submit_btn)

        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=15_000)

        verification_sel = (
            "input[name='pin'], .verification-code, input#input__email_verification_pin"
        )
        try:
            verification = await page.query_selector(verification_sel)
            if verification:
                logger.warning("linkedin_auth_requires_verification")
                return False
        except Exception:
            pass

        await behavior.reading_pause(1200)
        return await self.is_authenticated(page)

    # ------------------------------------------------------------------
    # URL construction
    # ------------------------------------------------------------------

    def _build_search_urls(self, criteria: SearchCriteria, config: DiscoveryConfig) -> list[str]:
        keyword_groups: list[str] = []

        if criteria.target_roles:
            for role in criteria.target_roles:
                words = role.split()[:_MAX_KEYWORDS_PER_GROUP]
                keyword_groups.append(" ".join(words))
        elif criteria.keywords:
            keyword_groups.append(" ".join(criteria.keywords[:_MAX_KEYWORDS_PER_GROUP]))

        if not keyword_groups:
            keyword_groups.append("software engineer")

        locations = list(criteria.locations) if criteria.locations else ["United States"]
        locations = locations[:3]

        remote_filter = "&f_WT=2" if criteria.remote_ok else ""

        urls: list[str] = []
        for kw in keyword_groups:
            for loc in locations:
                if len(urls) >= _MAX_SEARCH_URLS:
                    break
                urls.append(
                    f"https://www.linkedin.com/jobs/search/"
                    f"?keywords={quote_plus(kw)}"
                    f"&location={quote_plus(loc)}"
                    f"&f_TPR={_PAST_WEEK_FILTER}"
                    f"{remote_filter}"
                    f"&sortBy=DD"
                )
            if len(urls) >= _MAX_SEARCH_URLS:
                break

        return urls

    # ------------------------------------------------------------------
    # Search orchestration (with feed warm-up)
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
        """Visit the feed first (human warm-up), then run standard search."""
        try:
            await rate_limiter.wait()
            await page.goto(
                "https://www.linkedin.com/feed/",
                wait_until="domcontentloaded",
                timeout=20_000,
            )
            await behavior.reading_pause(2000)
            await behavior.scroll_down_naturally(page)
            await behavior.between_actions_pause(min_s=2.0, max_s=5.0)
        except Exception:
            logger.warning("linkedin_feed_warmup_failed", exc_info=True)

        return await super().run_search(
            page,
            criteria,
            config,
            behavior=behavior,
            rate_limiter=rate_limiter,
            captcha_handler=captcha_handler,
        )

    # ------------------------------------------------------------------
    # Card extraction
    # ------------------------------------------------------------------

    async def _extract_refs_from_page(self, page: Page) -> list[ListingRef]:
        from pipelines.job_agent.discovery.models import ListingRef

        try:
            await page.wait_for_selector(".jobs-search__results-list", timeout=10_000)
        except Exception:
            try:
                await page.wait_for_selector(
                    "[data-test='job-search-results'], .job-card-container",
                    timeout=5_000,
                )
            except Exception:
                logger.debug("linkedin_no_results_container")
                return []

        cards = await page.query_selector_all(".jobs-search__results-list li")
        if not cards:
            cards = await page.query_selector_all(".job-card-container--clickable")

        seen_ids: set[str] = set()
        refs: list[ListingRef] = []

        for card in cards:
            job_id = await self._extract_job_id_from_card(card)
            if not job_id or job_id in seen_ids:
                continue
            seen_ids.add(job_id)

            title = await self._extract_title(card)
            company = await self._extract_company(card)
            location = await self._extract_location(card)
            salary_text = await self._extract_salary(card)

            refs.append(
                ListingRef(
                    url=f"https://www.linkedin.com/jobs/view/{job_id}/",
                    title=title,
                    company=company,
                    source=JobSource.LINKEDIN,
                    location=location,
                    salary_text=salary_text,
                )
            )

        logger.debug("linkedin_extract", count=len(refs))
        return refs

    @staticmethod
    async def _extract_job_id_from_card(card: ElementHandle) -> str:
        """Pull the numeric job ID from a card via URN, attribute, or href."""
        try:
            urn = await card.get_attribute("data-entity-urn")
            if urn:
                m = _URN_RE.search(urn)
                if m:
                    return m.group(1)

            job_id = await card.get_attribute("data-job-id")
            if job_id and job_id.isdigit():
                return job_id

            links = await card.query_selector_all("a")
            for link in links:
                href = await link.get_attribute("href")
                if href:
                    extracted = extract_job_id_from_linkedin_url(href)
                    if extracted:
                        return extracted
        except Exception:
            pass
        return ""

    async def _extract_title(self, card: ElementHandle) -> str:
        for sel in (
            ".job-card-list__title",
            ".job-card-container__link",
            "a[aria-label]",
        ):
            text = await self._safe_text(await card.query_selector(sel))
            if text:
                return text
        try:
            link = await card.query_selector("a")
            if link:
                label = await link.get_attribute("aria-label")
                if label:
                    return label.strip()
        except Exception:
            pass
        return ""

    async def _extract_company(self, card: ElementHandle) -> str:
        for sel in (
            ".job-card-container__company-name",
            ".artdeco-entity-lockup__subtitle",
            ".job-card-list__company-name",
        ):
            text = await self._safe_text(await card.query_selector(sel))
            if text:
                return text
        return ""

    async def _extract_location(self, card: ElementHandle) -> str:
        """Find the metadata item that looks like a location, not a job type."""
        try:
            items = await card.query_selector_all(".job-card-container__metadata-item")
            for item in items:
                text = await self._safe_text(item)
                if not text:
                    continue
                text_lower = text.lower()
                if any(hint in text_lower for hint in _LOCATION_HINTS):
                    return text
                if len(text) > 3 and not text_lower.startswith("full"):
                    return text
        except Exception:
            pass
        return ""

    async def _extract_salary(self, card: ElementHandle) -> str:
        """Look for a metadata item containing salary indicators."""
        try:
            items = await card.query_selector_all(".job-card-container__metadata-item")
            for item in items:
                text = await self._safe_text(item)
                if text and ("$" in text or "K" in text):
                    return text
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------
    # Pagination (button + infinite scroll fallback)
    # ------------------------------------------------------------------

    def _next_page_selector(self) -> str | None:
        return 'button[aria-label="View next page"]'

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
        """LinkedIn-specific pagination: button primary, infinite scroll fallback."""
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
            # Delay BEFORE pagination interaction to avoid instant next request.
            await rate_limiter.wait()
            await behavior.between_pages_pause(self.source)
            advanced = await self._advance_page(page, behavior)
            if not advanced:
                break

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

    async def _advance_page(self, page: Page, behavior: HumanBehavior) -> bool:
        """Try next-page button, then fall back to infinite scroll."""
        try:
            next_btn = await page.query_selector('button[aria-label="View next page"]')
            if not next_btn:
                next_btn = await page.query_selector(
                    "li.artdeco-pagination__indicator--number[aria-current='true'] + li button"
                )
            if next_btn and await next_btn.is_visible():
                await behavior.human_click(page, next_btn)
                return True
        except Exception:
            pass

        try:
            await behavior.scroll_down_naturally(page)
            await asyncio.sleep(2.0)
            await behavior.between_actions_pause(min_s=0.5, max_s=1.5)
            return True
        except Exception:
            return False
