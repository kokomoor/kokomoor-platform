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
    if "indeed.com" in host and ("/viewjob" in path or "/rc/clk" in path):
        return "indeed"
    return None


async def _follow_apply_link(page: Page) -> str:
    """Click the Apply button and follow redirects to the actual form."""
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError
    import structlog
    logger = structlog.get_logger(__name__)

    # 1. Look for common Apply/Easy Apply buttons
    # LinkedIn/Indeed specific selectors plus generic fallbacks
    apply_selectors = [
        "button.jobs-apply-button",  # LinkedIn
        "[data-control-name*='apply']",
        ".jobs-apply-button",
        "button:has-text('Apply')",
        "a:has-text('Apply')",
        "button:has-text('Easy Apply')",
        "a:has-text('Easy Apply')",
        "#apply-button",
        ".apply-button",
    ]

    apply_btn = None
    for selector in apply_selectors:
        try:
            btn = await page.query_selector(selector)
            if btn and await btn.is_visible():
                apply_btn = btn
                break
        except (PlaywrightError, PlaywrightTimeoutError):
            continue

    if not apply_btn:
        return page.url

    # 2. Click and handle potential new tab
    try:
        async with page.context.expect_page(timeout=5000) as new_page_info:
            await apply_btn.click()
        new_page = await new_page_info.value
        await new_page.wait_for_load_state("domcontentloaded", timeout=5000)
        return new_page.url
    except PlaywrightTimeoutError:
        logger.warning("router.follow_apply_link.new_page_timeout", url=page.url)
        # Falls through to same-tab navigation or timeout
        pass
    except PlaywrightError as e:
        logger.warning("router.follow_apply_link.error", url=page.url, error=str(e))
        return page.url

    # 3. Wait for URL change in same tab
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
        return page.url
    except PlaywrightTimeoutError:
        logger.warning("router.follow_apply_link.same_tab_timeout", url=page.url)
        return page.url


async def route_application(
    listing: JobListing,
    *,
    page: Page | None = None,
) -> RouteDecision:
    """Determine the best submission strategy for a listing.

    First checks the listing URL. If that's a job board page (LinkedIn,
    Indeed) rather than a direct application URL, and a page is provided,
    navigate and follow the "Apply" link to discover the actual ATS.
    """
    initial_url = listing.url
    platform = detect_ats_platform(initial_url)
    
    # If it's a job board and we have a page, try to follow the apply link
    final_url = initial_url
    if platform in {"linkedin", "indeed"} and page:
        host, path = _parse_host_and_path(initial_url)
        # Only follow if it looks like a job VIEW page, not the form itself
        if "linkedin.com" in host and "/jobs/view/" in path:
            await page.goto(initial_url, wait_until="domcontentloaded")
            final_url = await _follow_apply_link(page)
            platform = detect_ats_platform(final_url)
        elif "indeed.com" in host and "/viewjob" in path:
            await page.goto(initial_url, wait_until="domcontentloaded")
            final_url = await _follow_apply_link(page)
            platform = detect_ats_platform(final_url)

    if platform == "greenhouse":
        return RouteDecision(
            strategy=SubmissionStrategy.API_GREENHOUSE,
            application_url=final_url,
            ats_platform="greenhouse",
            requires_browser=False,
            requires_account=False,
        )

    if platform == "lever":
        return RouteDecision(
            strategy=SubmissionStrategy.API_LEVER,
            application_url=final_url,
            ats_platform="lever",
            requires_browser=False,
            requires_account=False,
        )

    if platform == "linkedin":
        return RouteDecision(
            strategy=SubmissionStrategy.TEMPLATE_LINKEDIN_EASY_APPLY,
            application_url=final_url,
            ats_platform="linkedin",
            requires_browser=True,
            requires_account=True,
        )

    if platform == "ashby":
        return RouteDecision(
            strategy=SubmissionStrategy.TEMPLATE_ASHBY,
            application_url=final_url,
            ats_platform="ashby",
            requires_browser=True,
            requires_account=False,
        )

    if platform == "workday":
        return RouteDecision(
            strategy=SubmissionStrategy.AGENT_WORKDAY,
            application_url=final_url,
            ats_platform="workday",
            requires_browser=True,
            requires_account=True,
        )

    if platform in {"icims", "taleo", "smartrecruiters", "bamboohr", "indeed"}:
        return RouteDecision(
            strategy=SubmissionStrategy.AGENT_GENERIC,
            application_url=final_url,
            ats_platform=platform,
            requires_browser=True,
            requires_account=platform in {"icims", "taleo"},
        )

    return RouteDecision(
        strategy=SubmissionStrategy.AGENT_GENERIC,
        application_url=final_url,
        ats_platform="unknown",
        requires_browser=True,
        requires_account=False,
    )
