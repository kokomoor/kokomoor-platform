"""Vision Government Solutions property assessment portal wrapper.

VGSI runs ASP.NET Web Forms portals at ``gis.vgsi.com/<TownST>/Search.aspx``
for hundreds of municipalities.  Key challenges:

- **ASP.NET postback**: Form submission requires ``__VIEWSTATE``,
  ``__VIEWSTATEENCRYPTED``, ``__EVENTVALIDATION``, and
  ``__VIEWSTATEGENERATOR`` hidden fields.  These change on every page load.
- **Multi-town support**: Same portal structure, different base URLs.
  Town-specific behavior is encoded in the ``SiteProfile``.
- **Pagination**: Server-side DataGrid with postback-based page navigation
  (``__doPostBack('GridView1','Page$2')``).
- **No authentication required**: Public records, guest access.
- **Rate sensitivity**: Government sites are sensitive to load; conservative
  rate limits are mandatory.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import structlog
from bs4 import BeautifulSoup

from pipelines.scraper.models import (
    ErrorClassification,
    PaginationStrategy,
    ScrapeError,
)
from pipelines.scraper.wrappers.base import BaseSiteWrapper

if TYPE_CHECKING:
    from core.browser.actions import BrowserActions, NavigationResult
    from core.scraper.dedup import DedupEngine
    from core.scraper.fixtures import StructuralFingerprint
    from pipelines.scraper.models import SiteProfile

logger = structlog.get_logger(__name__)

_VIEWSTATE_FIELDS = [
    "__VIEWSTATE",
    "__VIEWSTATEENCRYPTED",
    "__VIEWSTATEGENERATOR",
    "__EVENTVALIDATION",
    "__EVENTTARGET",
    "__EVENTARGUMENT",
]


class VisionGSIWrapper(BaseSiteWrapper):
    """Wrapper for Vision Government Solutions property portals.

    Handles ASP.NET Web Forms postback mechanics (ViewState management,
    grid pagination) while inheriting stealth, rate limiting, and dedup
    from ``BaseSiteWrapper``.
    """

    def __init__(
        self,
        profile: SiteProfile,
        actions: BrowserActions,
        *,
        dedup: DedupEngine | None = None,
        reference_fingerprint: StructuralFingerprint | None = None,
    ) -> None:
        super().__init__(
            profile,
            actions,
            dedup=dedup,
            reference_fingerprint=reference_fingerprint,
        )
        self._viewstate_cache: dict[str, str] = {}

    async def _do_authenticate(self) -> bool:
        """VGSI portals are public — no auth required."""
        return True

    async def _do_extract_page(self) -> list[dict[str, Any]]:
        """Extract property records from the VGSI search results grid.

        VGSI uses an ASP.NET GridView with links to individual property
        detail pages.  We extract the summary data from the grid rows.
        """
        html = await self._actions.page.content()
        self._cache_viewstate(html)
        return self._extract_grid_rows(html)

    def _extract_grid_rows(self, html: str) -> list[dict[str, Any]]:
        """Parse property records from the ASP.NET GridView table."""
        soup = BeautifulSoup(html, "html.parser")

        grid = soup.select_one("table.GridStyle") or soup.select_one(
            "#MainContent_grdSearchResults"
        )
        if not grid:
            grid = soup.select_one("table#ctl00_MainContent_grdSearchResults")

        if not grid:
            selectors = self._profile.selectors
            if selectors.result_item:
                return self._extract_from_html(html)
            logger.debug("vgsi.no_grid_found", site_id=self.site_id)
            return []

        rows = grid.select("tr")
        if not rows:
            return []

        headers: list[str] = []
        header_row = rows[0]
        for th in header_row.select("th, td"):
            headers.append(th.get_text(strip=True).lower().replace(" ", "_"))

        records: list[dict[str, Any]] = []
        for row in rows[1:]:
            cells = row.select("td")
            if not cells or len(cells) != len(headers):
                continue

            pager_links = row.select("a[href*='__doPostBack']")
            if pager_links and len(cells) <= 2:
                continue

            record: dict[str, Any] = {}
            for header, cell in zip(headers, cells, strict=True):
                link = cell.select_one("a[href]")
                if link and link.get("href"):
                    href = str(link["href"])
                    if "Parcel.aspx" in href or "parcelid=" in href.lower():
                        record[header] = cell.get_text(strip=True)
                        record["detail_url"] = self._normalize_url(href)
                    else:
                        record[header] = cell.get_text(strip=True)
                else:
                    record[header] = cell.get_text(strip=True)

            if any(v for k, v in record.items() if k != "detail_url"):
                field_map = self._profile.selectors.field_map
                if field_map:
                    mapped: dict[str, Any] = {}
                    for target_field, source_col in field_map.items():
                        mapped[target_field] = record.get(source_col, "")
                    if "detail_url" in record:
                        mapped["detail_url"] = record["detail_url"]
                    records.append(mapped)
                else:
                    records.append(record)

        logger.debug(
            "vgsi.extracted_rows",
            site_id=self.site_id,
            rows=len(records),
        )
        return records

    async def _do_paginate(self, current_page: int) -> bool:
        """Navigate to the next page via ASP.NET postback.

        VGSI pagination uses ``__doPostBack('GridView1','Page$N')`` where N
        is the 1-based page number.
        """
        nav = self._profile.navigation
        if nav.pagination != PaginationStrategy.ASPNET_POSTBACK:
            return await super()._do_paginate(current_page)

        next_page = current_page + 1

        html = await self._actions.page.content()
        page_link_pattern = re.compile(
            r"__doPostBack\(['\"]([^'\"]+)['\"],\s*['\"]Page\$" + str(next_page) + r"['\"]\)"
        )
        match = page_link_pattern.search(html)
        if not match:
            logger.debug(
                "vgsi.no_next_page",
                site_id=self.site_id,
                current_page=current_page,
            )
            return False

        event_target = match.group(1)
        event_argument = f"Page${next_page}"

        try:
            await self._actions.page.evaluate(
                "([target, argument]) => __doPostBack(target, argument)",
                [event_target, event_argument],
            )
            await self._actions.page.wait_for_load_state("networkidle", timeout=15_000)
            await self._behavior.between_navigations_pause(min_s=1.0, max_s=3.0)

            new_html = await self._actions.page.content()
            self._cache_viewstate(new_html)

            logger.info(
                "vgsi.paginated",
                site_id=self.site_id,
                page=next_page,
            )
            return True
        except Exception as exc:
            self._errors.append(
                ScrapeError(
                    classification=ErrorClassification.SELECTOR,
                    message=f"ASP.NET pagination failed: {str(exc)[:300]}",
                    stage="paginate",
                )
            )
            return False

    def _cache_viewstate(self, html: str) -> None:
        """Extract and cache ASP.NET hidden fields for future postbacks."""
        soup = BeautifulSoup(html, "html.parser")
        for field_name in _VIEWSTATE_FIELDS:
            el = soup.select_one(f"input[name='{field_name}']")
            if el and el.get("value"):
                self._viewstate_cache[field_name] = str(el["value"])

    def _build_search_url(self, params: dict[str, Any], *, page: int = 1) -> str:
        """VGSI search is form-based, not URL-based. Return the search page."""
        template = self._profile.navigation.search_url_template
        try:
            return template.format(**{**params, "page": page})
        except KeyError:
            return template

    async def _do_navigate(self, url: str) -> NavigationResult:
        """Navigate and perform initial search via the ASP.NET form."""
        result = await self._actions.goto(url)
        if not result.success:
            return result

        html = await self._actions.page.content()
        self._cache_viewstate(html)
        return result

    def extract_from_fixture(self, html: str) -> list[dict[str, Any]]:
        """Override for offline fixture testing."""
        return self._extract_grid_rows(html)


def build_vgsi_profile(
    town_slug: str,
    *,
    display_name: str = "",
    search_mode: str = "street",
) -> dict[str, Any]:
    """Helper to generate a SiteProfile dict for a VGSI town.

    Common structure: ``gis.vgsi.com/<TownSlug>/Search.aspx``
    """
    base_url = f"https://gis.vgsi.com/{town_slug}"
    return {
        "site_id": f"vision_gsi_{town_slug.lower()}",
        "display_name": display_name or f"VGSI - {town_slug}",
        "base_url": base_url,
        "auth": {"type": "none"},
        "rate_limit": {
            "min_delay_s": 5.0,
            "max_delay_s": 12.0,
            "pages_before_long_pause": 5,
            "long_pause_min_s": 45.0,
            "long_pause_max_s": 120.0,
        },
        "requires_browser": True,
        "navigation": {
            "search_url_template": f"{base_url}/Search.aspx",
            "pagination": "aspnet_postback",
            "results_container_selector": "table.GridStyle, #MainContent_grdSearchResults",
        },
        "selectors": {
            "result_item": "table.GridStyle tr, #MainContent_grdSearchResults tr",
            "field_map": {},
        },
        "output_contract": {
            "fields": [
                {"name": "owner", "type": "str", "required": True},
                {"name": "address", "type": "str", "required": True},
                {"name": "mblu", "type": "str", "required": False},
                {"name": "assessment", "type": "str", "required": False},
                {"name": "detail_url", "type": "url", "required": False},
            ],
            "dedup_fields": ["owner", "address"],
            "min_records_per_search": 5,
            "max_empty_pages_before_stop": 2,
        },
        "fixture_refresh_days": 14,
        "drift_threshold": 0.80,
        "max_pages_per_search": 30,
        "notes": f"VGSI ASP.NET portal for {town_slug}. Public records, no auth required. Conservative rate limits.",
    }
