"""LinkedIn job search wrapper for the universal scraper.

Adapts the existing LinkedIn discovery provider logic to the new
``BaseSiteWrapper`` architecture.  Key site-specific behaviors:

- Multi-modal authentication (session restore → credential form)
- Guest fallback when auth fails
- JavaScript-rendered search results (requires browser)
- Infinite scroll pagination
- Aggressive bot detection (conservative rate limits mandatory)
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

import structlog
from bs4 import BeautifulSoup

from pipelines.scraper.wrappers.base import BaseSiteWrapper

if TYPE_CHECKING:
    from core.browser.actions import BrowserActions
    from core.scraper.dedup import DedupEngine
    from core.scraper.fixtures import StructuralFingerprint
    from pipelines.scraper.models import SiteProfile

logger = structlog.get_logger(__name__)

_JOB_CARD_SELECTORS = [
    "div.job-search-card",
    "li.jobs-search-results__list-item",
    "div.base-card",
    "li.result-card",
]


class LinkedInWrapper(BaseSiteWrapper):
    """LinkedIn job search wrapper.

    Handles LinkedIn's auth flow, JS-rendered pages, and aggressive
    bot detection while maintaining the BaseSiteWrapper lifecycle.
    """

    def __init__(
        self,
        profile: SiteProfile,
        actions: BrowserActions,
        *,
        dedup: DedupEngine | None = None,
        reference_fingerprint: StructuralFingerprint | None = None,
    ) -> None:
        super().__init__(profile, actions, dedup=dedup, reference_fingerprint=reference_fingerprint)
        self._is_guest_mode = False

    async def _do_authenticate(self) -> bool:
        """LinkedIn multi-modal auth: session → credentials → guest fallback."""
        auth = self._profile.auth
        if auth.type.value == "none":
            self._is_guest_mode = True
            return True

        result = await super()._do_authenticate()
        if not result:
            self._warnings.append("LinkedIn auth failed, falling back to guest mode")
            self._is_guest_mode = True
            self._errors = [e for e in self._errors if e.stage != "auth"]
            return True
        return True

    async def _do_extract_page(self) -> list[dict[str, Any]]:
        """Extract job listings from LinkedIn search results."""
        import asyncio

        await asyncio.sleep(random.uniform(1.2, 2.8))

        html = await self._actions.page.content()
        return self._extract_job_cards(html)

    def _extract_job_cards(self, html: str) -> list[dict[str, Any]]:
        """Parse job cards from LinkedIn HTML."""
        soup = BeautifulSoup(html, "html.parser")

        cards: list[Any] = []
        for selector in _JOB_CARD_SELECTORS:
            cards = soup.select(selector)
            if cards:
                break

        if not cards:
            return self._extract_from_html(html)

        records: list[dict[str, Any]] = []
        for card in cards:
            record: dict[str, Any] = {}

            title_el = (
                card.select_one("h3.base-search-card__title")
                or card.select_one("a.job-card-list__title")
                or card.select_one("h3.job-card-list__title")
                or card.select_one("h3")
            )
            if title_el:
                record["title"] = title_el.get_text(strip=True)

            company_el = (
                card.select_one("h4.base-search-card__subtitle")
                or card.select_one("a.job-card-container__company-name")
                or card.select_one("h4")
            )
            if company_el:
                record["company"] = company_el.get_text(strip=True)

            location_el = card.select_one("span.job-search-card__location") or card.select_one(
                "span.job-card-container__metadata-item"
            )
            if location_el:
                record["location"] = location_el.get_text(strip=True)

            link_el = card.select_one("a[href*='/jobs/']") or card.select_one("a[href]")
            if link_el and link_el.get("href"):
                record["url"] = self._normalize_url(str(link_el["href"]).split("?")[0])

            if record.get("title"):
                records.append(record)

        logger.debug(
            "linkedin.extracted",
            site_id=self.site_id,
            cards_found=len(cards),
            records=len(records),
            guest_mode=self._is_guest_mode,
        )
        return records

    async def _do_paginate(self, current_page: int) -> bool:
        """LinkedIn uses infinite scroll on authenticated pages."""
        import asyncio

        before_count = len(
            await self._actions.page.query_selector_all(", ".join(_JOB_CARD_SELECTORS))
        )

        for _ in range(3):
            await self._actions.scroll("down", amount=1200)
            await asyncio.sleep(random.uniform(1.0, 2.2))

        after_count = len(
            await self._actions.page.query_selector_all(", ".join(_JOB_CARD_SELECTORS))
        )

        if after_count > before_count:
            return True

        nav = self._profile.navigation
        if nav.next_button_selector:
            result = await self._actions.click(nav.next_button_selector)
            return result.success

        return False

    def extract_from_fixture(self, html: str) -> list[dict[str, Any]]:
        return self._extract_job_cards(html)
