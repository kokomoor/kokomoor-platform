"""LinkedIn Easy Apply template filler.

Drives the LinkedIn Easy Apply modal wizard, filling fields step-by-step
using the deterministic field mapper and LLM QA answerer.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from pipelines.job_agent.application._debug import capture_application_failure
from pipelines.job_agent.application.field_mapper import map_field
from pipelines.job_agent.application.qa_answerer import QACache, answer_application_question
from pipelines.job_agent.application.registry import register_submitter
from pipelines.job_agent.application.router import SubmissionStrategy
from pipelines.job_agent.application.templates._common import (
    get_field_label,
    select_option_fuzzy,
)
from pipelines.job_agent.models import ApplicationAttempt

if TYPE_CHECKING:
    import httpx
    from playwright.async_api import Page
    from core.browser.human_behavior import HumanBehavior
    from core.llm.protocol import LLMClient
    from pipelines.job_agent.models import (
        CandidateApplicationProfile,
        JobListing,
    )

logger = structlog.get_logger(__name__)

_STRATEGY = SubmissionStrategy.TEMPLATE_LINKEDIN_EASY_APPLY

# --- Selectors ---
#
# LinkedIn rewrites its CSS classes frequently but keeps aria-labels stable
# (accessibility lawsuits keep them honest). All primary selectors target
# aria-label; class-based selectors are kept only as fallbacks.
#
# The Easy Apply button renders as `button[aria-label="Easy Apply to <title>
# at <company>"]` on the job view page. A plain "Apply" button (external
# redirect, e.g. Stripe -> Greenhouse) has aria-label starting with "Apply"
# but without "Easy". We detect both and distinguish them in code.

_EASY_APPLY_BUTTON_SELECTORS = (
    "button[aria-label^='Easy Apply']",
    ".jobs-apply-button",
    ".jobs-s-apply button",
)

# Catches the non-Easy-Apply case (external redirect). The starts-with anchor
# keeps us from matching unrelated controls like "Apply filters" or "Apply
# location". This is a secondary probe — only used when Easy Apply selectors
# return nothing.
_APPLY_BUTTON_SELECTORS = (
    "button[aria-label^='Apply to']",
    "button[aria-label^='Apply on']",
    "button[aria-label='Apply']",
)

_MODAL_SELECTORS = (
    "div[role='dialog'][aria-labelledby*='easy-apply']",
    "div[role='dialog']",
    ".jobs-easy-apply-modal",
    ".artdeco-modal",
)

_NEXT_BUTTON_SELECTORS = (
    "button[aria-label='Continue to next step']",
    "button[aria-label='Review your application']",
    "footer button.artdeco-button--primary",
    ".artdeco-button--primary:not([aria-label*='Submit']):not([aria-label*='Dismiss'])",
)

_SUBMIT_BUTTON_SELECTORS = (
    "button[aria-label='Submit application']",
    "button[aria-label^='Submit']",
    "footer button[aria-label*='Submit']",
)

_FIELD_SELECTORS = (
    "input:not([type=hidden])",
    "select",
    "textarea",
    "fieldset",
)

# Selectors for the "Sign in to see who you know at <company>" overlay
# that LinkedIn injects on job pages even for authenticated users. It
# sits on top of the Apply button and makes it invisible to query_selector.
# The modal uses LinkedIn's artdeco-modal component; the dismiss button
# consistently carries aria-label="Dismiss" or the class
# artdeco-modal__dismiss. Checked against three observed occurrences.
_BLOCKER_MODAL_SELECTORS = (
    ".artdeco-modal",
    "div[role='dialog']",
)
_BLOCKER_MODAL_DISMISS_SELECTORS = (
    "button[aria-label='Dismiss']",
    "button.artdeco-modal__dismiss",
    "[data-test-modal-close-btn]",
)


# --- Daily Cap ---

def _get_daily_cap_file() -> Path:
    return Path("data/application_state/linkedin_daily_count.json")


def _check_daily_cap(max_cap: int) -> bool:
    """Issue 10: Robust daily cap check."""
    path = _get_daily_cap_file()
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if data.get("date") == today:
            return data.get("count", 0) >= max_cap
    except (json.JSONDecodeError, OSError):
        pass
    return False


def _increment_daily_cap() -> None:
    """Increment the daily application count via atomic temp-file swap.

    Called from a sync context inside the async submitter — must not
    invoke ``asyncio.run`` (there is already a running event loop).
    """
    path = _get_daily_cap_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now(UTC).strftime("%Y-%m-%d")

    for attempt in range(3):
        try:
            data: dict[str, object] = {"date": today, "count": 1}
            if path.exists():
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(existing, dict) and existing.get("date") == today:
                        existing["count"] = int(existing.get("count", 0)) + 1
                        data = existing
                except (json.JSONDecodeError, OSError):
                    pass

            temp_path = path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(data), encoding="utf-8")
            os.replace(temp_path, path)
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(0.1)


# --- Apply button locator ---


async def _locate_apply_button(page: Page) -> tuple[object | None, bool]:
    """Find the job's apply control and classify it.

    Returns:
        (element, is_easy_apply). ``element`` is ``None`` when neither an
        Easy Apply nor a plain Apply button can be found. ``is_easy_apply``
        distinguishes the LinkedIn modal flow from external ATS redirects
        when an element is found.
    """
    for sel in _EASY_APPLY_BUTTON_SELECTORS:
        btn = await page.query_selector(sel)
        if btn is None:
            continue
        if not await btn.is_visible():
            continue
        aria_label = (await btn.get_attribute("aria-label") or "").lower()
        text = (await btn.text_content() or "").strip().lower()
        # For class-based fallbacks the aria-label may not contain "easy",
        # so fall back to visible text when the attribute is inconclusive.
        if "easy apply" in aria_label or "easy apply" in text:
            return btn, True
        # Class-based fallback matched something that isn't labelled Easy
        # Apply; treat it as a plain Apply button and fall through to the
        # external-redirect path rather than blindly clicking.
        if "apply" in aria_label or "apply" in text:
            return btn, False

    for sel in _APPLY_BUTTON_SELECTORS:
        btn = await page.query_selector(sel)
        if btn is None:
            continue
        if not await btn.is_visible():
            continue
        return btn, False

    return None, False


# --- Blocker modal dismissal ---


async def _dismiss_blocker_modals(page: Page, behavior: HumanBehavior) -> bool:
    """Dismiss any overlay modal that appeared after navigation.

    LinkedIn's "Sign in to see who you already know at <company>" modal
    fires on job pages even for authenticated users and sits in front of
    the Apply button. It is NOT the Easy Apply wizard — it's a social-graph
    upsell. We detect it by checking for an open artdeco/dialog modal that
    carries a dismiss control, then click the X before any apply-button
    query runs.

    Returns True if a modal was found and dismissed.
    """
    for modal_sel in _BLOCKER_MODAL_SELECTORS:
        modal = await page.query_selector(modal_sel)
        if modal is None or not await modal.is_visible():
            continue
        for dismiss_sel in _BLOCKER_MODAL_DISMISS_SELECTORS:
            btn = await modal.query_selector(dismiss_sel)
            if btn is not None and await btn.is_visible():
                logger.info(
                    "linkedin.blocker_modal_dismissed",
                    modal_selector=modal_sel,
                    dismiss_selector=dismiss_sel,
                )
                await behavior.human_click(page, btn)
                await asyncio.sleep(0.8)
                return True
    return False


# --- Main Flow ---

async def fill_linkedin_easy_apply(
    listing: JobListing,
    profile: CandidateApplicationProfile,
    resume_path: Path,
    cover_letter_path: Path | None,
    *,
    client: httpx.AsyncClient | None = None,
    page: Page | None = None,
    llm: LLMClient | None = None,
    behavior: HumanBehavior | None = None,
    run_id: str = "",
    dry_run: bool = True,
    cache: QACache | None = None,
    max_daily_cap: int = 25,
) -> ApplicationAttempt:
    """Fill the LinkedIn Easy Apply modal wizard up to the submit step.

    Per the application engine architecture, the engine **never** clicks
    Submit on LinkedIn. When the wizard reaches the submit step the filler
    captures a screenshot, increments the daily-cap counter, and returns
    ``awaiting_review``. A human clicks Submit after inspecting the
    screenshot.
    """
    if page is None:
        raise ValueError("Page is required for LinkedIn Easy Apply.")

    if behavior is None:
        from core.browser.human_behavior import HumanBehavior as _HumanBehavior

        behavior = _HumanBehavior()

    if _check_daily_cap(max_daily_cap):
        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="stuck",
            strategy=_STRATEGY.value,
            summary=f"LinkedIn daily application cap ({max_daily_cap}) reached.",
        )

    await page.goto(listing.url, wait_until="domcontentloaded")
    await behavior.reading_pause(1000)

    # Dismiss the "Sign in to see who you know at <company>" overlay that
    # LinkedIn injects even for authenticated users on some job pages. It
    # sits on top of the Apply button and will cause _locate_apply_button
    # to find nothing. Dismissal is a no-op if no such modal is present.
    await _dismiss_blocker_modals(page, behavior)

    apply_btn, is_easy_apply = await _locate_apply_button(page)

    if not apply_btn:
        screenshot_path = await capture_application_failure(
            page, listing, run_id, "linkedin_template", "Easy Apply button not found"
        )
        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="error",
            strategy=_STRATEGY.value,
            summary="Easy Apply button not found on page.",
            screenshot_path=screenshot_path,
        )

    if not is_easy_apply:
        # External redirect — the listing uses the employer's own ATS
        # (Stripe -> Greenhouse, Datadog -> Lever, etc). The Easy Apply
        # template cannot drive that flow; the agent_generic fallback
        # should pick it up on the next run once routing is upgraded.
        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="stuck",
            strategy=_STRATEGY.value,
            summary="LinkedIn listing has an 'Apply' button (external redirect), not 'Easy Apply'.",
        )

    await behavior.human_click(page, apply_btn)

    # 3. Handle modal steps
    for step_idx in range(12): # Max 12 steps
        await asyncio.sleep(1.5) # Wait for modal/next step
        modal = None
        for sel in _MODAL_SELECTORS:
            modal = await page.query_selector(sel)
            if modal:
                break

        if not modal:
            logger.warning("linkedin.modal_disappeared", step=step_idx)
            break

        fields = await modal.query_selector_all(", ".join(_FIELD_SELECTORS))
        for field in fields:
            # Skip hidden or visually obscured fields
            if not await field.is_visible():
                continue

            label = await get_field_label(field, page)
            tag = await field.evaluate("el => el.tagName.toLowerCase()")
            field_type = await field.get_attribute("type") or "text"

            options = None
            if tag == "select":
                options = await field.evaluate(
                    "el => Array.from(el.options).map(o => o.text)"
                )

            mapping = map_field(label, field_type, options, profile)

            if mapping.confidence >= 0.8:
                if tag == "select":
                    await select_option_fuzzy(field, mapping.value, behavior)
                elif field_type == "file":
                    await field.set_input_files(str(resume_path))
                elif field_type in ("checkbox", "radio"):
                    is_checked = await field.is_checked()
                    if not is_checked:
                        await behavior.human_click(page, field)
                else:
                    await field.evaluate("el => el.value = ''")
                    await behavior.type_with_cadence(field, mapping.value)
            elif llm:
                qa_result = await answer_application_question(
                    llm=llm,
                    field_label=label,
                    field_type=field_type,
                    field_options=options,
                    candidate_profile=profile.model_dump_json(),
                    job_title=listing.title,
                    company=listing.company,
                    run_id=run_id,
                    cache=cache,
                )
                if tag == "select":
                    await select_option_fuzzy(field, qa_result.answer, behavior)
                elif field_type in ("checkbox", "radio"):
                    should_check = any(k in qa_result.answer.lower() for k in ["yes", "true", "check"])
                    is_checked = await field.is_checked()
                    if should_check != is_checked:
                        await behavior.human_click(page, field)
                else:
                    await field.evaluate("el => el.value = ''")
                    await behavior.type_with_cadence(field, qa_result.answer)

        # Reached the final step? Capture and stop — we never click Submit.
        submit_btn = None
        for sel in _SUBMIT_BUTTON_SELECTORS:
            submit_btn = await modal.query_selector(sel)
            if submit_btn and await submit_btn.is_visible():
                break
            submit_btn = None

        if submit_btn:
            screenshot_path = await capture_application_failure(
                page,
                listing,
                run_id,
                "linkedin_template",
                "LinkedIn Easy Apply ready for review",
                extra={"dry_run": dry_run},
            )
            _increment_daily_cap()
            return ApplicationAttempt(
                dedup_key=listing.dedup_key,
                status="awaiting_review",
                strategy=_STRATEGY.value,
                summary="LinkedIn Easy Apply filled and ready for human review.",
                screenshot_path=screenshot_path,
            )

        next_btn = None
        for sel in _NEXT_BUTTON_SELECTORS:
            next_btn = await modal.query_selector(sel)
            if next_btn and await next_btn.is_visible():
                break
            next_btn = None

        if not next_btn:
            break

        await behavior.human_click(page, next_btn)
        await behavior.between_actions_pause()

    screenshot_path = await capture_application_failure(
        page,
        listing,
        run_id,
        "linkedin_template",
        "LinkedIn Easy Apply wizard failed to reach submit step or disappeared",
    )
    return ApplicationAttempt(
        dedup_key=listing.dedup_key,
        status="error",
        strategy=_STRATEGY.value,
        summary="LinkedIn Easy Apply wizard failed to reach submit step or disappeared.",
        screenshot_path=screenshot_path,
    )

# Register the submitter
register_submitter(_STRATEGY, fill_linkedin_easy_apply)
