"""Workday ATS scraper.

Workday is used by many enterprise and defense companies. Every company
has a unique subdomain: {company}.wd1.myworkdayjobs.com (or wd5, wd12, etc.).
The UI structure is standardized -- one adapter works for all companies.

Company configuration format (KP_WORKDAY_TARGET_COMPANIES):
  "CompanyName:subdomain:wdN" e.g. "Raytheon:rtx:wd1,Northrop:northrop:wd5"
  If wdN is omitted, try wd1 first, then wd5.

Anti-detection notes:
- Workday uses aggressive bot detection including mouse movement tracking
  and timing analysis on form interactions.
- All search interactions MUST use HumanBehavior -- Workday's detection
  is particularly sensitive to programmatic input (zero-delay typing).
- Sessions help but Workday has shorter session lifetimes than LinkedIn.
  Expect re-auth more often (or use sessionless browsing if no auth required).
- Workday pages are React SPAs -- always wait for the job list element,
  not just DOMContentLoaded.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

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

_CAREER_PATHS = ("jobs", "External", "careers")
_DEFAULT_WD_VERSION = "wd1"
_FALLBACK_WD_VERSIONS = ("wd1", "wd5")


@dataclass(frozen=True)
class WorkdayTarget:
    """Parsed Workday company target from config."""

    company_name: str
    subdomain: str
    wd_version: str = _DEFAULT_WD_VERSION


def parse_workday_targets(raw_entries: list[str]) -> list[WorkdayTarget]:
    """Parse 'CompanyName:subdomain[:wdN]' config strings."""
    targets: list[WorkdayTarget] = []
    for entry in raw_entries:
        parts = entry.split(":")
        if len(parts) < 2:
            logger.warning("workday_invalid_target", entry=entry)
            continue
        company_name = parts[0].strip()
        subdomain = parts[1].strip()
        wd_version = parts[2].strip() if len(parts) >= 3 else _DEFAULT_WD_VERSION
        if company_name and subdomain:
            targets.append(
                WorkdayTarget(
                    company_name=company_name,
                    subdomain=subdomain,
                    wd_version=wd_version,
                )
            )
    return targets


class WorkdayProvider(BaseProvider):
    """Browser-based Workday ATS scraper.

    Iterates over configured company targets. Each target is a separate
    Workday career site with a standardized UI.
    """

    source: ClassVar[JobSource] = JobSource.WORKDAY

    def requires_auth(self) -> bool:
        return False

    def base_domain(self) -> str:
        return "myworkdayjobs.com"

    async def is_authenticated(self, page: Page) -> bool:
        return True

    def _build_search_urls(self, criteria: SearchCriteria, config: DiscoveryConfig) -> list[str]:
        return []

    async def _extract_refs_from_page(
        self, page: Page, *, company_name: str = ""
    ) -> list[ListingRef]:
        from pipelines.job_agent.discovery.models import ListingRef

        refs: list[ListingRef] = []
        cards = await page.query_selector_all("[data-automation-id='jobTitle']")

        for card in cards:
            title = await self._safe_text(card)
            if not title:
                continue

            link = await card.query_selector("a")
            if not link:
                parent = await card.evaluate_handle("el => el.closest('a')")
                parent_elem = parent.as_element() if parent else None
                link = parent_elem

            url = ""
            if link:
                href = await link.get_attribute("href")
                if href:
                    if href.startswith("/"):
                        current_url = page.url
                        from urllib.parse import urlparse

                        parsed = urlparse(current_url)
                        url = f"{parsed.scheme}://{parsed.netloc}{href}"
                    else:
                        url = href
                    url = canonicalize_url(url)

            if not url:
                continue

            location = ""
            for loc_sel in (
                "[data-automation-id='locations']",
                "[data-automation-id='descriptSubtitle']",
            ):
                loc_el = await self._find_sibling_or_nearby(card, loc_sel, page)
                if loc_el:
                    location = await self._safe_text(loc_el)
                    if location:
                        break

            refs.append(
                ListingRef(
                    url=url,
                    title=title,
                    company=company_name,
                    source=JobSource.WORKDAY,
                    location=location,
                )
            )

        logger.debug("workday_extract", count=len(refs))
        return refs

    @staticmethod
    async def _find_sibling_or_nearby(
        anchor: ElementHandle, selector: str, page: Page
    ) -> ElementHandle | None:
        """Find an element near the anchor using page-level query."""
        try:
            parent_handle = await anchor.evaluate_handle(
                "el => el.closest('li') || el.closest('[role=\"listitem\"]') || el.parentElement"
            )
            parent_elem = parent_handle.as_element() if parent_handle else None
            if parent_elem:
                found = await parent_elem.query_selector(selector)
                if found:
                    return found
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Search orchestration (per-company iteration)
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
        """Iterate over Workday company targets and aggregate results."""
        targets = parse_workday_targets(config.workday_companies)
        if not targets:
            logger.debug("workday_no_targets")
            return []

        all_refs: list[ListingRef] = []

        for target in targets:
            if len(all_refs) >= config.max_listings_per_provider:
                break

            company_refs = await self._search_company(
                page,
                target,
                criteria,
                config,
                behavior=behavior,
                rate_limiter=rate_limiter,
                captcha_handler=captcha_handler,
            )
            all_refs.extend(company_refs)

        return all_refs[: config.max_listings_per_provider]

    async def _search_company(
        self,
        page: Page,
        target: WorkdayTarget,
        criteria: SearchCriteria,
        config: DiscoveryConfig,
        *,
        behavior: HumanBehavior,
        rate_limiter: DomainRateLimiter,
        captcha_handler: CaptchaHandler,
    ) -> list[ListingRef]:
        """Search a single Workday company career site."""
        base_url = await self._resolve_career_url(page, target, rate_limiter=rate_limiter)
        if not base_url:
            logger.warning(
                "workday_career_url_not_found",
                company=target.company_name,
                subdomain=target.subdomain,
            )
            return []

        await behavior.between_actions_pause(min_s=1.0, max_s=2.5)

        keyword = ""
        if criteria.keywords:
            keyword = " ".join(criteria.keywords[:3])
        elif criteria.target_roles:
            keyword = criteria.target_roles[0]

        if keyword:
            filled = await self._fill_search(page, keyword, behavior=behavior)
            if not filled:
                logger.debug(
                    "workday_search_input_not_found",
                    company=target.company_name,
                )

        await self._wait_for_results(page)
        await behavior.simulate_interest_in_page(page)

        captcha = await captcha_handler.detect(page)
        if captcha.detected:
            outcome = await captcha_handler.handle(
                page,
                captcha,
                strategy=config.captcha_strategy,
                api_key=config.captcha_api_key.get_secret_value(),
            )
            if not outcome.resolved:
                return []

        refs = await self._extract_refs_from_page(page, company_name=target.company_name)

        for _ in range(config.max_pages_per_search - 1):
            if len(refs) >= config.max_listings_per_provider:
                break

            load_more = await page.query_selector("[data-automation-id='loadMoreButton']")
            if not load_more or not await load_more.is_visible():
                break

            # Delay BEFORE pagination interaction to avoid instant next request.
            await rate_limiter.wait()
            await behavior.between_pages_pause(self.source)
            await behavior.human_click(page, load_more)
            await asyncio.sleep(2.0)
            await behavior.simulate_interest_in_page(page)

            new_refs = await self._extract_refs_from_page(page, company_name=target.company_name)
            seen_urls = {r.url for r in refs}
            refs.extend(r for r in new_refs if r.url not in seen_urls)

        logger.info(
            "workday_search_complete",
            company=target.company_name,
            refs=len(refs),
        )
        return refs

    async def _resolve_career_url(
        self,
        page: Page,
        target: WorkdayTarget,
        *,
        rate_limiter: DomainRateLimiter,
    ) -> str:
        """Find the working career page URL for a Workday target.

        Tries career paths (/jobs, /External, /careers) and falls back
        to alternate wd versions if the configured one doesn't work.
        """
        wd_versions = [target.wd_version]
        if target.wd_version == _DEFAULT_WD_VERSION:
            wd_versions = list(_FALLBACK_WD_VERSIONS)

        for wd in wd_versions:
            for path in _CAREER_PATHS:
                url = f"https://{target.subdomain}.{wd}.myworkdayjobs.com/en-US/{path}"
                try:
                    await rate_limiter.wait()
                    resp = await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                    if resp and resp.ok:
                        has_jobs = await self._has_job_list(page)
                        if has_jobs:
                            logger.debug(
                                "workday_career_url_resolved",
                                url=url,
                                company=target.company_name,
                            )
                            return url
                except Exception:
                    continue
        return ""

    @staticmethod
    async def _has_job_list(page: Page) -> bool:
        """Check if the page has a recognizable Workday job list."""
        for sel in (
            "[data-automation-id='jobResults']",
            "[aria-label='Job Listings']",
            "[data-automation-id='jobTitle']",
        ):
            try:
                el = await page.query_selector(sel)
                if el:
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    async def _fill_search(
        page: Page,
        keyword: str,
        *,
        behavior: HumanBehavior,
    ) -> bool:
        """Fill the Workday search input with a keyword."""
        search_input = None
        for sel in (
            "input[placeholder='Search']",
            "input[aria-label='Search']",
            "input[data-automation-id='searchBox']",
        ):
            try:
                search_input = await page.query_selector(sel)
                if search_input:
                    break
            except Exception:
                continue

        if not search_input:
            return False

        await behavior.human_click(page, search_input)
        await behavior.between_actions_pause(min_s=0.3, max_s=0.8)
        await behavior.type_with_cadence(search_input, keyword)
        await behavior.between_actions_pause(min_s=0.2, max_s=0.4)
        await page.keyboard.press("Enter")
        await asyncio.sleep(2.0)
        return True

    @staticmethod
    async def _wait_for_results(page: Page) -> None:
        """Wait for Workday job results to render (React SPA)."""
        for sel in (
            "[data-automation-id='jobResults']",
            "[aria-label='Job Listings']",
        ):
            try:
                await page.wait_for_selector(sel, timeout=10_000)
                return
            except Exception:
                continue
        logger.debug("workday_results_timeout")
