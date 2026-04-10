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

Authentication modes handled:
  1. Already authenticated (session cookie valid) -- /feed/ or /jobs/ URL detected.
  2. Welcome Back (returning user) -- remembered account card, click to proceed
     to password-only entry.
  3. Standard login -- email + password fields on /login page.
  4. Password-only -- only password field visible (username pre-filled via cookie).
  5. Email verification / CAPTCHA challenge -- detected and reported, not retried.
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

    from core.browser.captcha import CaptchaHandler
    from core.browser.debug_capture import FailureCapture
    from core.browser.human_behavior import HumanBehavior
    from pipelines.job_agent.discovery.models import DiscoveryConfig, ListingRef
    from pipelines.job_agent.discovery.rate_limiter import DomainRateLimiter
    from pipelines.job_agent.models import SearchCriteria

logger = structlog.get_logger(__name__)

_MAX_SEARCH_URLS = 6
_MAX_KEYWORDS_PER_GROUP = 3
_PAST_WEEK_FILTER = "r604800"
_URN_RE = re.compile(r"urn:li:(?:job|jobPosting):(\d+)")
_JOB_ID_RE = re.compile(r"/jobs/view/[^?]*?(\d{6,})")
_LOCATION_HINTS = frozenset({",", "remote", "hybrid", "united states", "usa"})

# Selectors for results container -- LinkedIn changes these periodically.
# Authenticated page selectors
_RESULTS_CONTAINER_SELECTORS = (
    ".jobs-search__results-list",
    ".scaffold-layout__list",
    "[data-test='job-search-results']",
    ".job-card-container",
    ".jobs-search-results-list",
    # Guest page
    ".two-pane-serp-page__results-list",
    ".base-serp-page__content ul",
)

_CARD_SELECTORS = (
    # Authenticated
    ".jobs-search__results-list li",
    ".scaffold-layout__list li",
    ".job-card-container--clickable",
    "[data-job-id]",
    ".jobs-search-results-list__list-item",
    # Guest page
    ".base-search-card.job-search-card",
    "div.job-search-card[data-entity-urn]",
)

_TITLE_SELECTORS = (
    # Authenticated
    ".job-card-list__title",
    ".job-card-container__link",
    ".job-card-list__title--link",
    "a[aria-label]",
    ".artdeco-entity-lockup__title a",
    # Guest page
    "h3.base-search-card__title",
    ".base-search-card__title",
)

_COMPANY_SELECTORS = (
    # Authenticated
    ".job-card-container__company-name",
    ".artdeco-entity-lockup__subtitle",
    ".job-card-list__company-name",
    ".artdeco-entity-lockup__subtitle span",
    # Guest page
    "h4.base-search-card__subtitle a",
    "h4.base-search-card__subtitle",
    ".base-search-card__subtitle",
)


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
    # Authentication detection
    # ------------------------------------------------------------------

    async def is_authenticated(self, page: Page) -> bool:
        """Detect whether the current page state represents a logged-in session.

        Checks URL patterns first (cheapest), then DOM indicators. Returns
        True on first positive signal to avoid unnecessary DOM queries.
        """
        url_lower = page.url.lower()

        if "/login" in url_lower or "/checkpoint" in url_lower:
            return False

        if "/feed" in url_lower:
            return True
        if "/jobs/" in url_lower and "/jobs/guest/" not in url_lower:
            return True
        if "/in/" in url_lower or "/mynetwork" in url_lower:
            return True

        try:
            if await page.query_selector("input#username, input#password"):
                return False
        except Exception:
            pass

        nav_selectors = (
            ".global-nav__me-photo",
            ".global-nav",
            "a[href='/feed/']",
            "[data-control-name='identity_welcome_message']",
            "nav[aria-label*='primary'] a[href*='/in/']",
            "#global-nav",
        )
        for sel in nav_selectors:
            try:
                if await page.query_selector(sel):
                    return True
            except Exception:
                continue

        try:
            has_feed: bool = await page.evaluate(
                "document.querySelector('.feed-identity-module') !== null"
            )
            if has_feed:
                return True
        except Exception:
            pass

        return False

    # ------------------------------------------------------------------
    # Authentication flow (multi-mode)
    # ------------------------------------------------------------------

    async def authenticate(
        self,
        page: Page,
        *,
        email: str,
        password: str,
        behavior: HumanBehavior,
    ) -> bool:
        """Handle LinkedIn login across all observed auth modes."""
        if await self.is_authenticated(page):
            logger.info("linkedin_auth_skipped_already_authenticated", url=page.url)
            return True

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

        if await self.is_authenticated(page):
            logger.info("linkedin_auth_skipped_after_home_nav", url=page.url)
            return True

        # Mode 1: Welcome Back page -- remembered user card.
        if await self._handle_welcome_back(page, behavior):
            return await self._complete_password_entry(page, email, password, behavior)

        # Navigate to login page if not already there.
        if "/login" not in page.url.lower():
            await self._navigate_to_login(page, behavior)

        if await self.is_authenticated(page):
            logger.info("linkedin_auth_post_login_nav", url=page.url)
            return True

        # Mode 2: Password-only page (username pre-filled from cookie).
        if await self._is_password_only_page(page):
            logger.info("linkedin_auth_password_only_mode")
            return await self._fill_password_and_submit(page, password, behavior)

        # Mode 3: Standard full login (username + password).
        return await self._standard_login(page, email, password, behavior)

    async def _handle_welcome_back(self, page: Page, behavior: HumanBehavior) -> bool:
        """Detect and click through the 'Welcome Back' returning-user screen.

        LinkedIn's "remember me" flow shows a page at /login with a
        #rememberme-div container, a h1 "Welcome Back", and clickable
        profile buttons (button.member-profile__details) for each
        remembered account.  Clicking the profile button submits a hidden
        form that redirects to a password-only entry page.

        Returns True if the welcome-back page was detected and the
        remembered account was clicked.  Returns False if this is not a
        welcome-back page.
        """
        # Primary structural detection: the #rememberme-div container that
        # LinkedIn renders on the remember-me login page.
        container_selectors = (
            "#rememberme-div",
            ".memberList-container",
        )
        found_container = False
        for sel in container_selectors:
            try:
                if await page.query_selector(sel):
                    found_container = True
                    break
            except Exception:
                continue

        if not found_container:
            # Fallback: check page heading text.
            try:
                heading = await page.query_selector("h1.header__content__heading, h1")
                if heading:
                    text = (await heading.text_content() or "").strip().lower()
                    if "welcome back" in text:
                        found_container = True
            except Exception:
                pass

        if not found_container:
            return False

        logger.info("linkedin_welcome_back_detected", url=page.url)

        # Click the remembered account profile button.  The real selector
        # from LinkedIn's HTML: button.member-profile__details with an
        # aria-label like "Login as <Name>".
        account_selectors = (
            "button.member-profile__details",
            "button[aria-label^='Login as']",
            ".member-profile-container button",
            ".artdeco-list__item button",
            "#rememberme-div button",
        )
        for sel in account_selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    await behavior.between_actions_pause(min_s=0.8, max_s=2.0)
                    await behavior.human_click(page, el)
                    with contextlib.suppress(Exception):
                        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                    logger.info("linkedin_welcome_back_account_clicked", selector=sel)
                    return True
            except Exception:
                continue

        logger.warning("linkedin_welcome_back_no_account_button", url=page.url)
        return False

    async def _is_password_only_page(self, page: Page) -> bool:
        """Check if only the password field is present (no username)."""
        try:
            has_password = await page.query_selector("input#password")
            has_username = await page.query_selector("input#username")
            return has_password is not None and has_username is None
        except Exception:
            return False

    async def _complete_password_entry(
        self, page: Page, email: str, password: str, behavior: HumanBehavior
    ) -> bool:
        """After welcome-back click, handle whatever page appears next."""
        await behavior.reading_pause(600)

        if await self.is_authenticated(page):
            logger.info("linkedin_auth_after_welcome_back", url=page.url)
            return True

        if await self._is_password_only_page(page):
            return await self._fill_password_and_submit(page, password, behavior)

        try:
            username_input = await page.query_selector("input#username")
            if username_input:
                return await self._standard_login(page, email, password, behavior)
        except Exception:
            pass

        logger.warning("linkedin_auth_unexpected_state_after_welcome_back", url=page.url)
        return False

    async def _fill_password_and_submit(
        self, page: Page, password: str, behavior: HumanBehavior
    ) -> bool:
        """Fill password field and submit when username is pre-filled."""
        try:
            password_input = await page.wait_for_selector("input#password", timeout=8_000)
        except Exception:
            logger.warning("linkedin_password_field_not_found_timeout")
            return False

        if password_input is None:
            return False

        await behavior.human_click(page, password_input)
        await behavior.between_actions_pause(min_s=0.3, max_s=0.8)
        await behavior.type_with_cadence(password_input, password)
        await behavior.between_actions_pause(min_s=0.4, max_s=1.0)

        return await self._click_submit_and_verify(page, behavior)

    async def _standard_login(
        self, page: Page, email: str, password: str, behavior: HumanBehavior
    ) -> bool:
        """Full username + password login flow."""
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

        try:
            password_input = await page.wait_for_selector("input#password", timeout=5_000)
        except Exception:
            logger.warning("linkedin_password_field_not_found")
            return False

        if password_input is None:
            return False

        await behavior.human_click(page, password_input)
        await behavior.type_with_cadence(password_input, password)
        await behavior.between_actions_pause(min_s=0.4, max_s=1.2)

        return await self._click_submit_and_verify(page, behavior)

    async def _click_submit_and_verify(self, page: Page, behavior: HumanBehavior) -> bool:
        """Find submit button, click it, check for verification challenges."""
        submit_btn = None
        for sel in (
            "button[type='submit']",
            "button[aria-label='Sign in']",
            "button[data-litms-control-urn='login-submit']",
            ".login__form_action_container button",
        ):
            try:
                submit_btn = await page.query_selector(sel)
                if submit_btn:
                    break
            except Exception:
                continue

        if submit_btn is None:
            logger.warning("linkedin_submit_button_not_found")
            return False

        await behavior.hover_before_click(page, submit_btn)
        await behavior.between_actions_pause(min_s=0.2, max_s=0.5)
        await behavior.human_click(page, submit_btn)

        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=15_000)

        verification_selectors = (
            "input[name='pin']",
            ".verification-code",
            "input#input__email_verification_pin",
            "#app__container .two-step",
            "input[name='challenge_response']",
        )
        for sel in verification_selectors:
            try:
                if await page.query_selector(sel):
                    logger.warning("linkedin_auth_requires_verification", selector=sel)
                    return False
            except Exception:
                continue

        await behavior.reading_pause(1200)
        return await self.is_authenticated(page)

    async def _navigate_to_login(self, page: Page, behavior: HumanBehavior) -> None:
        """Navigate from homepage to login page via link or direct goto."""
        sign_in_btn = None
        for sel in (
            "a[data-tracking-control-name*='login']",
            "a[href*='/login']",
            "a.nav__button-secondary",
        ):
            try:
                sign_in_btn = await page.query_selector(sel)
                if sign_in_btn:
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

    # ------------------------------------------------------------------
    # URL construction
    # ------------------------------------------------------------------

    def _build_search_urls(self, criteria: SearchCriteria, config: DiscoveryConfig) -> list[str]:
        keyword_groups: list[str] = []

        if criteria.target_roles:
            for role in criteria.target_roles:
                words = role.split()[:_MAX_KEYWORDS_PER_GROUP]
                keyword_groups.append(" ".join(words))

        if criteria.keywords:
            for kw in criteria.keywords:
                words = kw.split()[:_MAX_KEYWORDS_PER_GROUP]
                group = " ".join(words)
                if group not in keyword_groups:
                    keyword_groups.append(group)

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
        capture: FailureCapture | None = None,
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
            capture=capture,
        )

    # ------------------------------------------------------------------
    # Card extraction (resilient multi-selector fallback)
    # ------------------------------------------------------------------

    async def _extract_refs_from_page(self, page: Page) -> list[ListingRef]:
        from pipelines.job_agent.discovery.models import ListingRef

        container_found = False
        for sel in _RESULTS_CONTAINER_SELECTORS:
            try:
                el = await page.wait_for_selector(sel, timeout=5_000)
                if el:
                    container_found = True
                    break
            except Exception:
                continue

        if not container_found:
            logger.debug("linkedin_no_results_container", url=page.url)
            return []

        cards: list[ElementHandle] = []
        for sel in _CARD_SELECTORS:
            try:
                cards = await page.query_selector_all(sel)
                if cards:
                    break
            except Exception:
                continue

        if not cards:
            logger.debug("linkedin_no_cards_found", url=page.url)
            return []

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
        """Pull the numeric job ID from a card via URN, attribute, or href.

        Handles both authenticated pages (data on the card element) and
        guest pages (data on a child div, URN format urn:li:jobPosting:ID).
        """
        try:
            # Direct attribute on card element.
            urn = await card.get_attribute("data-entity-urn")
            if urn:
                m = _URN_RE.search(urn)
                if m:
                    return m.group(1)

            job_id = await card.get_attribute("data-job-id")
            if job_id and job_id.isdigit():
                return job_id

            # Check child elements (guest page: <li> wraps <div> with the URN).
            child = await card.query_selector("[data-entity-urn]")
            if child:
                child_urn = await child.get_attribute("data-entity-urn")
                if child_urn:
                    m = _URN_RE.search(child_urn)
                    if m:
                        return m.group(1)

            # Regex scan of outerHTML as last-resort for embedded IDs.
            outer = await card.evaluate("el => el.outerHTML.slice(0, 2000)")
            if outer:
                m = _URN_RE.search(outer)
                if m:
                    return m.group(1)
                m = _JOB_ID_RE.search(outer)
                if m:
                    return m.group(1)

            # Extract from href of any link in the card.
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
        for sel in _TITLE_SELECTORS:
            text = await self._safe_text(await card.query_selector(sel))
            if text:
                return text
        try:
            link = await card.query_selector("a")
            if link:
                label = await link.get_attribute("aria-label")
                if label:
                    return label.strip()
                text = await self._safe_text(link)
                if text:
                    return text
        except Exception:
            pass
        return ""

    async def _extract_company(self, card: ElementHandle) -> str:
        for sel in _COMPANY_SELECTORS:
            text = await self._safe_text(await card.query_selector(sel))
            if text:
                return text
        return ""

    async def _extract_location(self, card: ElementHandle) -> str:
        """Find the metadata item that looks like a location, not a job type."""
        # Guest page has a dedicated location span.
        for sel in (".job-search-card__location",):
            text = await self._safe_text(await card.query_selector(sel))
            if text:
                return text

        location_selectors = (
            ".job-card-container__metadata-item",
            ".artdeco-entity-lockup__caption",
            ".job-card-container__metadata-wrapper li",
            ".base-search-card__metadata span",
        )
        for container_sel in location_selectors:
            try:
                items = await card.query_selector_all(container_sel)
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
                continue
        return ""

    async def _extract_salary(self, card: ElementHandle) -> str:
        """Look for a metadata item containing salary indicators."""
        salary_selectors = (
            ".job-card-container__metadata-item",
            ".artdeco-entity-lockup__caption",
            ".job-search-card__salary-info",
            ".base-search-card__metadata span",
        )
        for container_sel in salary_selectors:
            try:
                items = await card.query_selector_all(container_sel)
                for item in items:
                    text = await self._safe_text(item)
                    if text and ("$" in text or "K" in text):
                        return text
            except Exception:
                continue
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
        capture: FailureCapture | None = None,
    ) -> list[ListingRef]:
        """LinkedIn-specific pagination: button primary, infinite scroll fallback."""
        try:
            await rate_limiter.wait()
            await page.goto(start_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            logger.warning("navigation_failed", url=start_url, exc_info=True)
            return []

        if not await self._verify_search_page(page, start_url):
            if capture:
                await capture.capture_page_failure(
                    source=self.source,
                    stage="search_page_verification_failed",
                    reason="linkedin_search_redirected_or_blocked",
                    page=page,
                    extra={"expected_url": start_url},
                )
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

        if not refs and capture:
            await capture.capture_page_failure(
                source=self.source,
                stage="linkedin_zero_results",
                reason="linkedin_extraction_returned_zero_refs",
                page=page,
                extra={"url": start_url},
            )

        page_count = 1

        for _ in range(config.max_pages_per_search - 1):
            await rate_limiter.wait()
            await behavior.between_navigations_pause()
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

    async def _verify_search_page(self, page: Page, expected_url: str) -> bool:
        """Verify that a search navigation landed on a real results page."""
        url_lower = page.url.lower()
        if "/login" in url_lower or "/checkpoint" in url_lower:
            logger.warning(
                "linkedin_search_redirected_to_login",
                expected=expected_url,
                actual=page.url,
            )
            return False
        if "/authwall" in url_lower:
            logger.warning(
                "linkedin_search_hit_authwall",
                expected=expected_url,
                actual=page.url,
            )
            return False
        return True

    async def _advance_page(self, page: Page, behavior: HumanBehavior) -> bool:
        """Try next-page button, then fall back to infinite scroll."""
        next_selectors = (
            'button[aria-label="View next page"]',
            "li.artdeco-pagination__indicator--number[aria-current='true'] + li button",
            ".artdeco-pagination__button--next",
        )
        for sel in next_selectors:
            try:
                next_btn = await page.query_selector(sel)
                if next_btn and await next_btn.is_visible():
                    await behavior.human_click(page, next_btn)
                    return True
            except Exception:
                continue

        try:
            await behavior.scroll_down_naturally(page)
            await asyncio.sleep(2.0)
            await behavior.between_actions_pause(min_s=0.5, max_s=1.5)
            return True
        except Exception:
            return False
