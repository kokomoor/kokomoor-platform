"""Wellfound (formerly AngelList Talent) scraper.

Wellfound requires login for most useful filters (salary, stage, equity).
Without login, the public search returns limited results.

Auth flow: email + password login. Wellfound also supports Google OAuth --
do NOT use it (requires additional browser interaction and is harder to automate
reliably). Stick to email/password.

Anti-detection notes:
- Wellfound is a React SPA -- all content is JS-rendered. Page loads must
  wait for the job list to appear, not just DOMContentLoaded.
- Wellfound watches for session age. Sessions are valuable here: an
  established session with login history looks human. SessionStore is
  MANDATORY for Wellfound -- never use it without a valid session if one exists.
- After login, navigate away from the login page and back (via home page)
  before searching -- this is normal human behavior after logging in.
"""

from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

import structlog

from pipelines.job_agent.discovery.providers.base import BaseProvider
from pipelines.job_agent.discovery.url_utils import canonicalize_url
from pipelines.job_agent.models import JobSource

if TYPE_CHECKING:
    from typing import ClassVar

    from playwright.async_api import Page

    from pipelines.job_agent.discovery.human_behavior import HumanBehavior
    from pipelines.job_agent.discovery.models import DiscoveryConfig, ListingRef
    from pipelines.job_agent.models import SearchCriteria

logger = structlog.get_logger(__name__)

_MAX_URL_COMBOS = 3
_WELLFOUND_JOB_RE = re.compile(r"/jobs/([^/]+)/at/([^/?#]+)")


class WellfoundProvider(BaseProvider):
    """Browser-based Wellfound job scraper (requires login)."""

    source: ClassVar[JobSource] = JobSource.WELLFOUND

    def requires_auth(self) -> bool:
        return True

    def base_domain(self) -> str:
        return "wellfound.com"

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def is_authenticated(self, page: Page) -> bool:
        """Check if the current session has a logged-in Wellfound user."""
        try:
            await page.goto(
                "https://wellfound.com",
                wait_until="domcontentloaded",
                timeout=20_000,
            )
        except Exception:
            logger.warning("wellfound_auth_check_failed", exc_info=True)
            return False

        if "/login" in page.url:
            return False

        for selector in (
            ".user-avatar",
            "[data-test='user-avatar']",
            "[aria-label='User menu']",
        ):
            try:
                el = await page.query_selector(selector)
                if el:
                    return True
            except Exception:
                continue

        return False

    async def authenticate(
        self,
        page: Page,
        *,
        email: str,
        password: str,
        behavior: HumanBehavior,
    ) -> bool:
        """Perform email/password login on Wellfound."""
        try:
            await page.goto(
                "https://wellfound.com/login",
                wait_until="domcontentloaded",
                timeout=20_000,
            )
        except Exception:
            logger.warning("wellfound_login_nav_failed", exc_info=True)
            return False

        try:
            email_input = await page.wait_for_selector(
                "input[name='email'], input[type='email']", timeout=10_000
            )
        except Exception:
            logger.warning("wellfound_email_field_not_found")
            return False

        if email_input is None:
            return False

        await behavior.human_click(page, email_input)
        await behavior.type_with_cadence(email_input, email)
        await behavior.between_actions_pause()

        password_input = await page.query_selector("input[name='password'], input[type='password']")
        if password_input is None:
            logger.warning("wellfound_password_field_not_found")
            return False

        await behavior.human_click(page, password_input)
        await behavior.type_with_cadence(password_input, password)
        await behavior.between_actions_pause()

        submit = await page.query_selector(
            "button[type='submit'], button[data-test='login-button']"
        )
        if submit is None:
            logger.warning("wellfound_submit_button_not_found")
            return False

        await behavior.human_click(page, submit)

        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=15_000)

        await behavior.reading_pause(500)

        mfa_input = await page.query_selector(
            "input[placeholder*='code'], input[name='otp'], input[data-test='mfa-input']"
        )
        if mfa_input:
            logger.warning("wellfound_mfa_required")
            return False

        return await self.is_authenticated(page)

    # ------------------------------------------------------------------
    # URL construction
    # ------------------------------------------------------------------

    def _build_search_urls(self, criteria: SearchCriteria, config: DiscoveryConfig) -> list[str]:
        location = criteria.locations[0] if criteria.locations else ""
        remote = "1" if criteria.remote_ok else "0"
        urls: list[str] = []

        if criteria.target_roles:
            for role in criteria.target_roles:
                if len(urls) >= _MAX_URL_COMBOS:
                    break
                urls.append(self._make_search_url(role, location, remote))
        elif criteria.keywords:
            query = " ".join(criteria.keywords[:4])
            urls.append(self._make_search_url(query, location, remote))
        else:
            urls.append(self._make_search_url("", location, remote))

        return urls[:_MAX_URL_COMBOS]

    @staticmethod
    def _make_search_url(query: str, location: str, remote: str) -> str:
        url = f"https://wellfound.com/jobs?q={quote_plus(query)}"
        if location:
            url += f"&location={quote_plus(location)}"
        url += f"&remote={remote}"
        return url

    # ------------------------------------------------------------------
    # Card extraction
    # ------------------------------------------------------------------

    async def _extract_refs_from_page(self, page: Page) -> list[ListingRef]:
        from pipelines.job_agent.discovery.models import ListingRef

        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=10_000)

        links = await page.query_selector_all("a[href*='/jobs/'][href*='/at/']")
        refs: list[ListingRef] = []
        seen_urls: set[str] = set()

        for link in links:
            try:
                href = await link.get_attribute("href")
            except Exception:
                continue
            if not href:
                continue

            match = _WELLFOUND_JOB_RE.search(href)
            if not match:
                continue

            if not href.startswith("http"):
                href = f"https://wellfound.com{href}"

            url = canonicalize_url(href)
            if url in seen_urls:
                continue
            seen_urls.add(url)

            role_slug = match.group(1)
            company_slug = match.group(2)

            title = await self._safe_text(link)
            if not title:
                title = role_slug.replace("-", " ").title()

            company = company_slug.replace("-", " ").title()
            try:
                parent = await link.evaluate_handle("el => el.closest('[data-test]')")
                parent_elem = parent.as_element() if parent else None
                if parent_elem:
                    comp_el = await parent_elem.query_selector(
                        "[data-test='StartupResult'] h2, [data-test='company-name']"
                    )
                    if comp_el:
                        extracted = await self._safe_text(comp_el)
                        if extracted:
                            company = extracted
            except Exception:
                pass

            location = ""
            try:
                parent_handle = await link.evaluate_handle("el => el.closest('div[class]')")
                parent_div = parent_handle.as_element() if parent_handle else None
                if parent_div:
                    spans = await parent_div.query_selector_all("span")
                    for span in spans:
                        text = await self._safe_text(span)
                        text_lower = text.lower()
                        if any(
                            kw in text_lower
                            for kw in (
                                "remote",
                                "new york",
                                "san francisco",
                                "seattle",
                                ",",
                            )
                        ):
                            location = text
                            break
            except Exception:
                pass

            refs.append(
                ListingRef(
                    url=url,
                    title=title,
                    company=company,
                    source=JobSource.WELLFOUND,
                    location=location,
                )
            )

        logger.debug("wellfound_extract", count=len(refs))
        return refs

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def _next_page_selector(self) -> str | None:
        return "[aria-label='Go to next page']"
