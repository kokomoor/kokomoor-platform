"""Ashby template filler.

Drives the Ashby single-page job application form.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

import structlog

from pipelines.job_agent.application.field_mapper import map_field
from pipelines.job_agent.application.qa_answerer import answer_application_question, QACache
from pipelines.job_agent.application.registry import register_submitter
from pipelines.job_agent.application.router import SubmissionStrategy
from pipelines.job_agent.application.templates._common import get_field_label, select_option_fuzzy
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

_STRATEGY = SubmissionStrategy.TEMPLATE_ASHBY

# --- Selectors ---

_FIELD_SELECTORS = (
    "input:not([type=hidden])",
    "select",
    "textarea",
)

_SUBMIT_BUTTON_SELECTORS = (
    "button[type='submit']",
    "button:has-text('Submit Application')",
    "button:has-text('Apply')",
)


async def fill_ashby_application(
    listing: JobListing,
    profile: CandidateApplicationProfile,
    resume_path: Path,
    cover_letter_path: Path | None,
    *,
    client: httpx.AsyncClient | None = None,
    page: Page | None = None,
    llm: LLMClient | None = None,
    behavior: Optional[HumanBehavior] = None,
    run_id: str = "",
    dry_run: bool = True,
    cache: Optional[QACache] = None,
    **kwargs: Optional[object],
) -> ApplicationAttempt:
    """Fill Ashby single-page application form."""
    if page is None:
        raise ValueError("Page is required for Ashby template.")
    
    if behavior is None:
        from core.browser.human_behavior import HumanBehavior
        behavior = HumanBehavior()

    # 1. Navigate to listing
    await page.goto(listing.url, wait_until="domcontentloaded")
    await behavior.reading_pause(1000)

    # 2. Extract and fill fields
    fields = await page.query_selector_all(", ".join(_FIELD_SELECTORS))
    fields_filled = 0
    llm_calls_made = 0

    for field in fields:
        # Skip if not visible or disabled
        if not await field.is_visible() or await field.is_disabled():
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
                if not await field.is_checked():
                    await behavior.human_click(page, field)
            else:
                await field.evaluate("el => el.value = ''")
                await behavior.type_with_cadence(field, mapping.value)
            fields_filled += 1
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
            fields_filled += 1
            llm_calls_made += 1

    # 3. Take screenshot and return awaiting review
    from pipelines.job_agent.application._debug import capture_application_failure
    screenshot_path = await capture_application_failure(
        page, listing, run_id, "ashby_template", 
        "Ashby form filled and ready for review",
        extra={"fields_filled": fields_filled, "llm_calls_made": llm_calls_made}
    )

    return ApplicationAttempt(
        dedup_key=listing.dedup_key,
        status="awaiting_review",
        strategy=_STRATEGY.value,
        summary="Ashby form filled and ready for review.",
        screenshot_path=screenshot_path,
        fields_filled=fields_filled,
        llm_calls_made=llm_calls_made,
    )

# Register the submitter
register_submitter(_STRATEGY, fill_ashby_application)
