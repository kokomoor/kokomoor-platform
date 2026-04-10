"""Lever Postings public JSON API provider.

Endpoint: GET https://api.lever.co/v0/postings/{slug}?mode=json
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

_API_BASE = "https://api.lever.co/v0/postings"


class LeverProvider:
    """Fetch job listings from a single Lever company postings page."""

    source: ClassVar[JobSource] = JobSource.LEVER

    def __init__(self, company_slug: str) -> None:
        self._slug = company_slug
        self._company_display = company_slug.replace("-", " ").title()

    def requires_auth(self) -> bool:
        return False

    def base_domain(self) -> str:
        return "api.lever.co"

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
        """Fetch all postings from the Lever board and filter by criteria."""
        from pipelines.job_agent.discovery.models import ListingRef

        url = f"{_API_BASE}/{self._slug}?mode=json"
        try:
            fetcher = HttpFetcher(timeout_seconds=15.0)
            postings = await fetcher.fetch_json(url)
        except Exception:
            logger.warning("lever_fetch_failed", slug=self._slug, exc_info=True)
            return []

        if not isinstance(postings, list):
            logger.warning("lever_unexpected_response", slug=self._slug)
            return []

        refs: list[ListingRef] = []

        for posting in postings:
            title = posting.get("text", "")
            if not matches_criteria(title, criteria):
                continue
            hosted_url = posting.get("hostedUrl", "")
            if not hosted_url:
                continue
            categories = posting.get("categories") or {}
            location = categories.get("location", "") if isinstance(categories, dict) else ""

            refs.append(
                ListingRef(
                    url=canonicalize_url(hosted_url),
                    title=title,
                    company=self._company_display,
                    source=JobSource.LEVER,
                    location=location,
                )
            )
            if len(refs) >= config.max_listings_per_provider:
                break

        logger.info(
            "lever_fetch_complete",
            slug=self._slug,
            total_postings=len(postings),
            matched=len(refs),
        )
        return refs


async def fetch_all_lever_companies(
    companies: list[str],
    criteria: SearchCriteria,
    config: DiscoveryConfig,
) -> list[ListingRef]:
    """Fetch jobs from multiple Lever company boards concurrently."""
    tasks = [
        LeverProvider(slug).run_search(
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
                "lever_provider_failed",
                slug=slug,
                error=str(result)[:200],
            )
        else:
            refs.extend(result)
    return refs
