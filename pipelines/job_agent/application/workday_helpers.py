"""Workday-specific application helpers.

Handles account wall detection and resume pre-fill verification which are
common friction points on the Workday platform.
"""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page
    from pipelines.job_agent.models import CandidateApplicationProfile

logger = structlog.get_logger(__name__)


async def detect_workday_account_wall(page: Page) -> bool:
    """Check if we've hit a Workday sign-in/account-creation wall.

    Workday often requires an account. Since we don't automate account
    creation, we must detect this and signal 'stuck'.
    """
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError
    
    selectors = [
        "div[data-automation-id='signInForm']",
        "div[data-automation-id='createAccountForm']",
        "form[data-automation-id='loginForm']",
        "input[data-automation-id='email']", # Sign-in email field
        "button[data-automation-id='signInSubmitButton']",
    ]

    for selector in selectors:
        try:
            # Wait up to 2 seconds for the wall to appear
            if await page.wait_for_selector(selector, state="attached", timeout=2000):
                logger.info("workday.account_wall_detected", selector=selector)
                return True
        except (PlaywrightTimeoutError, PlaywrightError):
            continue
    return False


async def verify_workday_prefill(
    page: Page,
    profile: CandidateApplicationProfile,
) -> list[str]:
    """Verify that Workday's resume-parsing correctly filled standard fields.

    Workday often pre-fills data from the uploaded resume. We should
    verify at least the core identity fields.
    """
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError
    mismatches = []

    # Mapping of Workday automation IDs to profile attributes
    checks = [
        ("input[data-automation-id='legalNameSection_firstName']", profile.personal.first_name),
        ("input[data-automation-id='legalNameSection_lastName']", profile.personal.last_name),
        ("input[data-automation-id='emailInput']", profile.personal.email),
        ("input[data-automation-id='phoneInput']", profile.personal.phone_formatted),
    ]

    for selector, expected in checks:
        if not expected:
            continue
        try:
            el = await page.wait_for_selector(selector, state="attached", timeout=2000)
            if el:
                val = await el.get_attribute("value") or ""
                if expected.lower() not in val.lower() and val.lower() not in expected.lower():
                    mismatches.append(selector.split("_")[-1].replace("']", ""))
        except (PlaywrightTimeoutError, PlaywrightError):
            continue

    if mismatches:
        logger.warning("workday.prefill_mismatches", fields=mismatches)
    return mismatches
