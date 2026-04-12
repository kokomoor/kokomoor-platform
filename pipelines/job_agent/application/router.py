"""Application router: choose submission strategy from a listing URL."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from playwright.async_api import Page

    from pipelines.job_agent.models import JobListing


class SubmissionStrategy(StrEnum):
    """Supported application submission strategies."""

    API_GREENHOUSE = "api_greenhouse"
    API_LEVER = "api_lever"
    TEMPLATE_LINKEDIN_EASY_APPLY = "template_linkedin_easy_apply"
    TEMPLATE_ASHBY = "template_ashby"
    AGENT_WORKDAY = "agent_workday"
    AGENT_GENERIC = "agent_generic"


@dataclass(frozen=True)
class RouteDecision:
    """Result of routing a listing to an application strategy."""

    strategy: SubmissionStrategy
    application_url: str
    ats_platform: str
    requires_browser: bool
    requires_account: bool


def _parse_host_and_path(url: str) -> tuple[str, str]:
    """Best-effort host/path parse for absolute and scheme-less URLs."""
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if host:
        return host, path

    if parsed.scheme and parsed.path:
        # Handles malformed inputs like "https:foo.bar/baz".
        return parsed.scheme.lower(), parsed.path.lower()

    if not parsed.scheme and parsed.path and "." in parsed.path:
        reparsed = urlparse(f"https://{parsed.path}")
        return reparsed.netloc.lower(), reparsed.path.lower()

    return "", ""


def detect_ats_platform(url: str) -> str | None:
    """Detect ATS platform from URL pattern."""
    host, path = _parse_host_and_path(url)
    if not host:
        return None

    if "greenhouse" in host or "boards.greenhouse.io" in host:
        return "greenhouse"
    if "lever" in host or "jobs.lever.co" in host:
        return "lever"
    if "myworkdayjobs" in host or "myworkday" in host:
        return "workday"
    if "icims" in host:
        return "icims"
    if "taleo" in host or "taleo.net" in host:
        return "taleo"
    if "ashbyhq" in host or "jobs.ashbyhq.com" in host:
        return "ashby"
    if "smartrecruiters" in host:
        return "smartrecruiters"
    if "bamboohr" in host:
        return "bamboohr"
    if "linkedin.com" in host and "/jobs/" in path:
        return "linkedin"
    return None


async def route_application(
    listing: JobListing,
    *,
    page: Page | None = None,
) -> RouteDecision:
    """Determine strategy for a listing using URL-only inference.

    In this prompt, browser navigation and redirect-chain following are
    intentionally deferred. The ``page`` parameter is accepted for API
    compatibility with later prompts but is unused.
    """
    _ = page

    platform = detect_ats_platform(listing.url)

    if platform == "greenhouse":
        return RouteDecision(
            strategy=SubmissionStrategy.API_GREENHOUSE,
            application_url=listing.url,
            ats_platform="greenhouse",
            requires_browser=False,
            requires_account=False,
        )

    if platform == "lever":
        return RouteDecision(
            strategy=SubmissionStrategy.API_LEVER,
            application_url=listing.url,
            ats_platform="lever",
            requires_browser=False,
            requires_account=False,
        )

    if platform == "linkedin":
        return RouteDecision(
            strategy=SubmissionStrategy.TEMPLATE_LINKEDIN_EASY_APPLY,
            application_url=listing.url,
            ats_platform="linkedin",
            requires_browser=True,
            requires_account=True,
        )

    if platform == "ashby":
        return RouteDecision(
            strategy=SubmissionStrategy.TEMPLATE_ASHBY,
            application_url=listing.url,
            ats_platform="ashby",
            requires_browser=True,
            requires_account=False,
        )

    if platform == "workday":
        return RouteDecision(
            strategy=SubmissionStrategy.AGENT_WORKDAY,
            application_url=listing.url,
            ats_platform="workday",
            requires_browser=True,
            requires_account=True,
        )

    if platform in {"icims", "taleo", "smartrecruiters", "bamboohr"}:
        return RouteDecision(
            strategy=SubmissionStrategy.AGENT_GENERIC,
            application_url=listing.url,
            ats_platform=platform,
            requires_browser=True,
            requires_account=platform in {"icims", "taleo"},
        )

    return RouteDecision(
        strategy=SubmissionStrategy.AGENT_GENERIC,
        application_url=listing.url,
        ats_platform="unknown",
        requires_browser=True,
        requires_account=False,
    )
