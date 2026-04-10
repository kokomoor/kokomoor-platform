"""Greenhouse Boards public JSON API provider.

Endpoint: GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false
No authentication required. No browser needed.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar

import structlog

from core.fetch import HttpFetcher
from pipelines.job_agent.discovery.url_utils import canonicalize_url, matches_criteria
from pipelines.job_agent.models import JobSource

if TYPE_CHECKING:
    from playwright.async_api import Page

    from pipelines.job_agent.discovery.captcha import CaptchaHandler
    from pipelines.job_agent.discovery.human_behavior import HumanBehavior
    from pipelines.job_agent.discovery.models import DiscoveryConfig, ListingRef
    from pipelines.job_agent.discovery.rate_limiter import DomainRateLimiter
    from pipelines.job_agent.models import SearchCriteria

logger = structlog.get_logger(__name__)

_API_BASE = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseProvider:
    """Fetch job listings from a single Greenhouse company board."""

    source: ClassVar[JobSource] = JobSource.GREENHOUSE

    def __init__(self, company_slug: str) -> None:
        self._slug = company_slug
        self._company_display = company_slug.replace("-", " ").title()

    def requires_auth(self) -> bool:
        return False

    def base_domain(self) -> str:
        return "boards-api.greenhouse.io"

    async def is_authenticated(self, page: Page) -> bool:
        return True

    async def authenticate(
        self,
        page: Page,
        *,
        email: str,
        password: str,
        behavior: Any,
    ) -> bool:
        return True

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
        """Fetch all jobs from the Greenhouse board and filter by criteria."""
        from pipelines.job_agent.discovery.models import ListingRef

        url = f"{_API_BASE}/{self._slug}/jobs?content=false"
        try:
            fetcher = HttpFetcher(timeout_seconds=15.0)
            data = await fetcher.fetch_json(url)
        except Exception:
            logger.warning("greenhouse_fetch_failed", slug=self._slug, exc_info=True)
            return []

        jobs: list[dict[str, Any]] = data.get("jobs", [])
        refs: list[ListingRef] = []

        for job in jobs:
            title = job.get("title", "")
            if not matches_criteria(title, criteria):
                continue
            absolute_url = job.get("absolute_url", "")
            if not absolute_url:
                continue
            location_obj = job.get("location") or {}
            location = location_obj.get("name", "") if isinstance(location_obj, dict) else ""

            refs.append(
                ListingRef(
                    url=canonicalize_url(absolute_url),
                    title=title,
                    company=self._company_display,
                    source=JobSource.GREENHOUSE,
                    location=location,
                )
            )
            if len(refs) >= config.max_listings_per_provider:
                break

        logger.info(
            "greenhouse_fetch_complete",
            slug=self._slug,
            total_jobs=len(jobs),
            matched=len(refs),
        )
        return refs


async def fetch_all_greenhouse_companies(
    companies: list[str],
    criteria: SearchCriteria,
    config: DiscoveryConfig,
) -> list[ListingRef]:
    """Fetch jobs from multiple Greenhouse company boards concurrently."""
    tasks = [
        GreenhouseProvider(slug).run_search(
            None,  # type: ignore[arg-type]  # HTTP provider — no browser page needed
            criteria,
            config,
            behavior=None,  # type: ignore[arg-type]  # not used by HTTP providers
            rate_limiter=None,  # type: ignore[arg-type]  # not used by HTTP providers
            captcha_handler=None,  # type: ignore[arg-type]  # not used by HTTP providers
        )
        for slug in companies
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    refs: list[ListingRef] = []
    for slug, result in zip(companies, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning(
                "greenhouse_provider_failed",
                slug=slug,
                error=str(result)[:200],
            )
        else:
            refs.extend(result)
    return refs
