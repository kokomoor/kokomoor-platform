"""US Land Records portal wrapper.

USLandRecords.com is a multi-vendor platform operated by Avenu Insights &
Analytics.  Rhode Island municipalities each have their own portal instance
(e.g., ``uslandrecords.com/RI/<Town>/``), using one of several backend
systems:

- **Laredo**: JSP-based document search
- **Kofile/GovOS**: Newer portal with different selectors
- **USLandRecords native**: Legacy platform

Key challenges:
- **Multi-vendor routing**: Different towns use different backends; the
  wrapper detects which vendor and adjusts selectors accordingly.
- **Session management**: Some portals require accepting a terms-of-use
  page before searching.
- **Document-level records**: Results are individual recorded documents
  (deeds, mortgages, liens), not properties — different dedup strategy.
- **Paid access**: Some features (document images) require payment; we
  extract index data only (free).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import structlog
from bs4 import BeautifulSoup

from pipelines.scraper.wrappers.base import BaseSiteWrapper

if TYPE_CHECKING:
    from core.browser.actions import BrowserActions, NavigationResult
    from core.scraper.dedup import DedupEngine
    from core.scraper.fixtures import StructuralFingerprint
    from pipelines.scraper.models import SiteProfile

logger = structlog.get_logger(__name__)


class VendorType:
    LAREDO = "laredo"
    KOFILE = "kofile"
    NATIVE = "native"
    UNKNOWN = "unknown"


class USLandRecordsWrapper(BaseSiteWrapper):
    """Wrapper for US Land Records property document portals.

    Auto-detects the backend vendor (Laredo, Kofile, native) and adjusts
    extraction selectors accordingly.
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
        self._vendor: str = VendorType.UNKNOWN
        self._accepted_terms = False

    async def _do_authenticate(self) -> bool:
        """No account auth, but may need to accept terms-of-use."""
        return True

    async def _do_navigate(self, url: str) -> NavigationResult:
        """Navigate and handle terms-of-use acceptance if needed."""
        result = await self._actions.goto(url)
        if not result.success:
            return result

        html = await self._actions.page.content()
        self._detect_vendor(html)

        if not self._accepted_terms:
            await self._accept_terms_if_present(html)

        return result

    def _detect_vendor(self, html: str) -> None:
        """Detect which backend vendor this portal instance uses."""
        html_lower = html.lower()
        if "laredo" in html_lower or "laredoapp" in html_lower:
            self._vendor = VendorType.LAREDO
        elif "kofile" in html_lower or "govos" in html_lower:
            self._vendor = VendorType.KOFILE
        elif "uslandrecords" in html_lower or "uslr" in html_lower:
            self._vendor = VendorType.NATIVE
        else:
            self._vendor = VendorType.UNKNOWN

        logger.info("uslandrecords.vendor_detected", vendor=self._vendor, site_id=self.site_id)

    async def _accept_terms_if_present(self, html: str) -> None:
        """Click through terms-of-use/disclaimer page if present."""
        terms_selectors = [
            "input[value='I Accept']",
            "button:has-text('Accept')",
            "a:has-text('I Accept')",
            "#btnAccept",
            "input[type='submit'][value*='Accept']",
        ]
        for selector in terms_selectors:
            result = await self._actions.click(selector)
            if result.success:
                self._accepted_terms = True
                logger.info("uslandrecords.terms_accepted", site_id=self.site_id)
                await self._actions.page.wait_for_load_state("networkidle", timeout=10_000)
                return

    async def _do_extract_page(self) -> list[dict[str, Any]]:
        """Extract document records from the search results page."""
        html = await self._actions.page.content()
        return self._extract_document_records(html)

    def _extract_document_records(self, html: str) -> list[dict[str, Any]]:
        """Parse document index records from the results table."""
        soup = BeautifulSoup(html, "html.parser")

        results_table = (
            soup.select_one("table.searchResults")
            or soup.select_one("#searchResultsTable")
            or soup.select_one("table.datagrid")
        )

        if not results_table:
            fallback_selectors = self._profile.selectors
            if fallback_selectors.result_item:
                return self._extract_from_html(html)
            return []

        rows = results_table.select("tr")
        if not rows:
            return []

        headers: list[str] = []
        for th in rows[0].select("th, td"):
            raw = th.get_text(strip=True).lower()
            normalized = re.sub(r"\s+", "_", raw)
            normalized = re.sub(r"[^a-z0-9_]", "", normalized)
            headers.append(normalized)

        records: list[dict[str, Any]] = []
        for row in rows[1:]:
            cells = row.select("td")
            if not cells or len(cells) != len(headers):
                continue

            record: dict[str, Any] = {}
            for header, cell in zip(headers, cells, strict=True):
                text = cell.get_text(strip=True)
                link = cell.select_one("a[href]")
                if link and link.get("href"):
                    record[header] = text
                    record["doc_url"] = self._normalize_url(str(link["href"]))
                else:
                    record[header] = text

            field_map = self._profile.selectors.field_map
            if field_map and any(v for v in record.values()):
                mapped: dict[str, Any] = {}
                for target, source in field_map.items():
                    mapped[target] = record.get(source, "")
                if "doc_url" in record:
                    mapped["doc_url"] = record["doc_url"]
                records.append(mapped)
            elif any(v for v in record.values()):
                records.append(record)

        logger.debug(
            "uslandrecords.extracted",
            site_id=self.site_id,
            vendor=self._vendor,
            records=len(records),
        )
        return records

    def extract_from_fixture(self, html: str) -> list[dict[str, Any]]:
        """Override for offline fixture testing."""
        self._detect_vendor(html)
        return self._extract_document_records(html)


def build_uslandrecords_profile(
    town_slug: str,
    state: str = "RI",
    *,
    display_name: str = "",
    portal_base: str = "",
) -> dict[str, Any]:
    """Helper to generate a SiteProfile dict for a US Land Records town."""
    if not portal_base:
        portal_base = f"https://i2l.uslandrecords.com/{state}/{town_slug}"

    return {
        "site_id": f"uslandrecords_{state.lower()}_{town_slug.lower()}",
        "display_name": display_name or f"US Land Records - {town_slug}, {state}",
        "base_url": portal_base,
        "auth": {"type": "none"},
        "rate_limit": {
            "min_delay_s": 5.0,
            "max_delay_s": 15.0,
            "pages_before_long_pause": 4,
            "long_pause_min_s": 60.0,
            "long_pause_max_s": 180.0,
        },
        "requires_browser": True,
        "navigation": {
            "search_url_template": f"{portal_base}/searchentry.aspx",
            "pagination": "next_button",
            "next_button_selector": "a.nextPage, input[value='Next'], a:has-text('Next')",
            "results_container_selector": "table.searchResults, #searchResultsTable",
            "no_results_indicator": "No documents found",
        },
        "selectors": {
            "result_item": "table.searchResults tr, #searchResultsTable tr",
            "field_map": {},
        },
        "output_contract": {
            "fields": [
                {
                    "name": "doc_type",
                    "type": "str",
                    "required": True,
                    "description": "Document type (deed, mortgage, lien, etc.)",
                },
                {
                    "name": "grantor",
                    "type": "str",
                    "required": True,
                    "description": "Party granting interest",
                },
                {
                    "name": "grantee",
                    "type": "str",
                    "required": True,
                    "description": "Party receiving interest",
                },
                {
                    "name": "record_date",
                    "type": "date",
                    "required": False,
                    "description": "Recording date",
                },
                {
                    "name": "book_page",
                    "type": "str",
                    "required": False,
                    "description": "Book and page reference",
                },
                {
                    "name": "consideration",
                    "type": "str",
                    "required": False,
                    "description": "Transaction amount",
                },
                {
                    "name": "doc_url",
                    "type": "url",
                    "required": False,
                    "description": "Link to document viewer",
                },
            ],
            "dedup_fields": ["grantor", "grantee", "record_date", "book_page"],
            "min_records_per_search": 3,
            "max_empty_pages_before_stop": 2,
        },
        "fixture_refresh_days": 14,
        "drift_threshold": 0.80,
        "max_pages_per_search": 50,
        "notes": f"US Land Records portal for {town_slug}, {state}. Terms acceptance may be required. Document-level records, not property-level.",
    }
