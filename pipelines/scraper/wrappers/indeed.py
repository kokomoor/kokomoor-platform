"""Indeed job search wrapper for the universal scraper.

Adapts Indeed job search to the ``BaseSiteWrapper`` architecture.
Key characteristics:

- No authentication required (public search)
- Server-rendered HTML with some JS hydration
- Standard pagination (URL parameter based)
- Moderate bot detection (reasonable rate limits)
"""

from __future__ import annotations

from typing import Any

import structlog
from bs4 import BeautifulSoup

from pipelines.scraper.wrappers.base import BaseSiteWrapper

logger = structlog.get_logger(__name__)

_JOB_CARD_SELECTORS = [
    "div.job_seen_beacon",
    "div.jobsearch-ResultsList div.result",
    "li.css-1ac2h1w",
    "div.cardOutline",
    "td.resultContent",
]


class IndeedWrapper(BaseSiteWrapper):
    """Indeed job search wrapper."""

    async def _do_authenticate(self) -> bool:
        """Indeed is public — no auth needed."""
        return True

    async def _do_extract_page(self) -> list[dict[str, Any]]:
        """Extract job listings from Indeed search results."""
        html = await self._actions.page.content()
        return self._extract_job_cards(html)

    def _extract_job_cards(self, html: str) -> list[dict[str, Any]]:
        """Parse job cards from Indeed HTML."""
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
                card.select_one("h2.jobTitle a")
                or card.select_one("a[data-jk]")
                or card.select_one("h2 a")
            )
            if title_el:
                record["title"] = title_el.get_text(strip=True)
                href = title_el.get("href")
                if href:
                    record["url"] = self._normalize_url(str(href))

            company_el = (
                card.select_one("span.css-1h7lukg")
                or card.select_one("[data-testid='company-name']")
                or card.select_one("span.companyName")
            )
            if company_el:
                record["company"] = company_el.get_text(strip=True)

            location_el = (
                card.select_one("div.css-1restlb")
                or card.select_one("[data-testid='text-location']")
                or card.select_one("div.companyLocation")
            )
            if location_el:
                record["location"] = location_el.get_text(strip=True)

            salary_el = card.select_one("div.salary-snippet-container") or card.select_one(
                "[data-testid='attribute_snippet_testid']"
            )
            if salary_el:
                record["salary"] = salary_el.get_text(strip=True)

            if record.get("title"):
                records.append(record)

        logger.debug(
            "indeed.extracted",
            site_id=self.site_id,
            cards_found=len(cards),
            records=len(records),
        )
        return records

    def extract_from_fixture(self, html: str) -> list[dict[str, Any]]:
        return self._extract_job_cards(html)
