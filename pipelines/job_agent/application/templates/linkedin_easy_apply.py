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

_EASY_APPLY_BUTTON_SELECTORS = (
    "button.jobs-apply-button",
    "[data-control-name*='inapply']",
    ".jobs-s-apply button",
)

_MODAL_SELECTORS = (
    ".jobs-easy-apply-modal",
    "div[data-test-modal-id='easy-apply-modal']",
    ".artdeco-modal",
)

_NEXT_BUTTON_SELECTORS = (
    "button[aria-label='Continue to next step']",
    "button[aria-label='Review your application']",
    "button.artdeco-button--primary",
)

_SUBMIT_BUTTON_SELECTORS = (
    "button[aria-label='Submit application']",
    "footer button.artdeco-button--primary",
)

_FIELD_SELECTORS = (
    "input:not([type=hidden])",
    "select",
    "textarea",
    "fieldset",
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

    apply_btn = None
    for sel in _EASY_APPLY_BUTTON_SELECTORS:
        apply_btn = await page.query_selector(sel)
        if apply_btn:
            break

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

    btn_text = (await apply_btn.text_content() or "").strip().lower()
    if "easy apply" not in btn_text and "apply" in btn_text:
        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="stuck",
            strategy=_STRATEGY.value,
            summary="Button is 'Apply', not 'Easy Apply'. External redirect not handled here.",
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

        next_btn = None
        for sel in _NEXT_BUTTON_SELECTORS:
            next_btn = await modal.query_selector(sel)
            if next_btn:
                break

        if not next_btn:
            break

        btn_text = (await next_btn.text_content() or "").strip().lower()
        if "submit" in btn_text:
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
