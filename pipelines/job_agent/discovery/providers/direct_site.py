"""Direct career-site scraper for companies not on a standard ATS.

Some high-priority companies (e.g., defense contractors) run proprietary
career sites. This provider uses a YAML config file to define per-site
scrape targets with custom selectors.

Config file format (KP_DIRECT_SITE_CONFIGS points to this YAML)::

    sites:
      - name: "Anduril Industries"
        url: "https://jobs.anduril.com/jobs"
        source: "company_site"
        job_card_selector: "[data-job-listing]"
        title_selector: "h2.job-title"
        company_name: "Anduril Industries"
        location_selector: ".job-location"
        link_selector: "a.job-link"
        pagination_selector: "a[rel='next']"
        requires_js: true
        search_via_url_params: true
        url_param_key: "q"

Each site entry produces a configured scrape target. The provider
iterates through all configured sites and returns all matching refs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote_plus, urljoin

import structlog

from pipelines.job_agent.discovery.providers.base import BaseProvider
from pipelines.job_agent.discovery.url_utils import canonicalize_url
from pipelines.job_agent.models import JobSource

if TYPE_CHECKING:
    from typing import ClassVar

    from playwright.async_api import Page

    from pipelines.job_agent.discovery.captcha import CaptchaHandler
    from pipelines.job_agent.discovery.debug_capture import FailureCapture
    from pipelines.job_agent.discovery.human_behavior import HumanBehavior
    from pipelines.job_agent.discovery.models import DiscoveryConfig, ListingRef
    from pipelines.job_agent.discovery.rate_limiter import DomainRateLimiter
    from pipelines.job_agent.models import SearchCriteria

logger = structlog.get_logger(__name__)


@dataclass
class DirectSiteTarget:
    """A single career-site scrape target loaded from YAML config."""

    name: str
    url: str
    company_name: str
    job_card_selector: str
    title_selector: str = "h2"
    location_selector: str = ""
    link_selector: str = "a"
    pagination_selector: str = ""
    requires_js: bool = True
    search_via_url_params: bool = False
    url_param_key: str = "q"
    source: str = "company_site"


def _load_site_configs(config_path: str) -> list[DirectSiteTarget]:
    """Load and validate site configs from a YAML file."""
    import yaml

    path = Path(config_path)
    if not path.is_file():
        logger.warning("direct_site_config_not_found", path=config_path)
        return []

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("direct_site_config_parse_error", path=config_path, exc_info=True)
        return []

    if not isinstance(data, dict) or "sites" not in data:
        logger.warning("direct_site_config_missing_sites_key", path=config_path)
        return []

    targets: list[DirectSiteTarget] = []
    for entry in data["sites"]:
        if not isinstance(entry, dict):
            continue
        required = ("name", "url", "job_card_selector")
        if not all(entry.get(k) for k in required):
            logger.warning("direct_site_config_incomplete_entry", entry=entry)
            continue
        targets.append(
            DirectSiteTarget(
                name=entry["name"],
                url=entry["url"],
                company_name=str(entry.get("company_name") or entry["name"]),
                job_card_selector=entry["job_card_selector"],
                title_selector=entry.get("title_selector", "h2"),
                location_selector=entry.get("location_selector", ""),
                link_selector=entry.get("link_selector", "a"),
                pagination_selector=entry.get("pagination_selector", ""),
                requires_js=entry.get("requires_js", True),
                search_via_url_params=entry.get("search_via_url_params", False),
                url_param_key=entry.get("url_param_key", "q"),
                source=entry.get("source", "company_site"),
            )
        )

    logger.debug("direct_site_configs_loaded", count=len(targets))
    return targets


class DirectSiteProvider(BaseProvider):
    """Scraper for proprietary career sites configured via YAML."""

    source: ClassVar[JobSource] = JobSource.COMPANY_SITE

    def __init__(self) -> None:
        self._targets: list[DirectSiteTarget] = []
        self._current_target: DirectSiteTarget | None = None

    def requires_auth(self) -> bool:
        return False

    def base_domain(self) -> str:
        return ""

    async def is_authenticated(self, page: Page) -> bool:
        return True

    def _build_search_urls(
        self,
        criteria: SearchCriteria,
        config: DiscoveryConfig,
        *,
        target: DirectSiteTarget | None = None,
    ) -> list[str]:
        target = target or self._current_target
        if target is None:
            return []

        if not target.search_via_url_params:
            return [target.url]

        keywords = criteria.keywords[:3] if criteria.keywords else []
        if not keywords and criteria.target_roles:
            keywords = [criteria.target_roles[0]]

        if not keywords:
            return [target.url]

        urls: list[str] = []
        for kw in keywords:
            sep = "&" if "?" in target.url else "?"
            urls.append(f"{target.url}{sep}{target.url_param_key}={quote_plus(kw)}")
        return urls

    async def _extract_refs_from_page(
        self, page: Page, *, target: DirectSiteTarget | None = None
    ) -> list[ListingRef]:
        from pipelines.job_agent.discovery.models import ListingRef

        target = target or self._current_target
        if target is None:
            return []

        if target.requires_js:
            await asyncio.sleep(3.0)

        cards = await page.query_selector_all(target.job_card_selector)
        if not cards:
            logger.debug("direct_site_no_cards", site=target.name)
            return []

        refs: list[ListingRef] = []
        seen_urls: set[str] = set()

        for card in cards:
            title_el = await card.query_selector(target.title_selector)
            title = await self._safe_text(title_el)
            if not title:
                continue

            link_el = await card.query_selector(target.link_selector)
            href = ""
            if link_el:
                href = (await link_el.get_attribute("href")) or ""

            if not href:
                continue

            if href.startswith("/"):
                href = urljoin(page.url, href)
            url = canonicalize_url(href)

            if url in seen_urls:
                continue
            seen_urls.add(url)

            location = ""
            if target.location_selector:
                loc_el = await card.query_selector(target.location_selector)
                location = await self._safe_text(loc_el)

            refs.append(
                ListingRef(
                    url=url,
                    title=title,
                    company=target.company_name,
                    source=JobSource.COMPANY_SITE,
                    location=location,
                )
            )

        logger.debug("direct_site_extract", site=target.name, count=len(refs))
        return refs

    def _next_page_selector(self, target: DirectSiteTarget | None = None) -> str | None:
        t = target or self._current_target
        if t and t.pagination_selector:
            return t.pagination_selector
        return None

    # ------------------------------------------------------------------
    # Search orchestration (per-site iteration)
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
        """Iterate over configured direct sites and aggregate results."""
        if not config.direct_site_configs:
            logger.debug("direct_site_no_config_path")
            return []

        self._targets = _load_site_configs(config.direct_site_configs)
        if not self._targets:
            logger.debug("direct_site_no_targets_loaded")
            return []

        all_refs: list[ListingRef] = []

        for target in self._targets:
            if len(all_refs) >= config.max_listings_per_provider:
                break

            self._current_target = target
            logger.info("direct_site_scraping", site=target.name, url=target.url)

            site_refs = await self._scrape_site(
                page,
                target,
                criteria,
                config,
                behavior=behavior,
                rate_limiter=rate_limiter,
                captcha_handler=captcha_handler,
            )
            all_refs.extend(site_refs)

        self._current_target = None
        return all_refs[: config.max_listings_per_provider]

    async def _scrape_site(
        self,
        page: Page,
        target: DirectSiteTarget,
        criteria: SearchCriteria,
        config: DiscoveryConfig,
        *,
        behavior: HumanBehavior,
        rate_limiter: DomainRateLimiter,
        captcha_handler: CaptchaHandler,
    ) -> list[ListingRef]:
        """Scrape a single direct career site."""
        search_urls = self._build_search_urls(criteria, config)

        refs: list[ListingRef] = []
        for url in search_urls:
            page_refs = await super()._run_single_search(
                page,
                url,
                config,
                behavior=behavior,
                rate_limiter=rate_limiter,
                captcha_handler=captcha_handler,
            )
            refs.extend(page_refs)
            if len(refs) >= config.max_listings_per_provider:
                break

        logger.info(
            "direct_site_complete",
            site=target.name,
            refs=len(refs),
        )
        return refs
