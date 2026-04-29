"""Generic LLM Agent-based application filler.

Uses ``WebAgentController`` to drive any application form that doesn't
have a dedicated API or template strategy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import structlog

from core.web_agent.controller import WebAgentController
from core.web_agent.protocol import AgentGoal, AgentResult
from pipelines.job_agent.application.qa_answerer import _safe_format
from pipelines.job_agent.application.registry import register_submitter
from pipelines.job_agent.application.router import SubmissionStrategy
from pipelines.job_agent.models import ApplicationAttempt

if TYPE_CHECKING:
    import httpx
    from playwright.async_api import Page
    from core.llm.protocol import LLMClient
    from pipelines.job_agent.models import (
        CandidateApplicationProfile,
        JobListing,
    )

logger = structlog.get_logger(__name__)


def _load_agent_system_template() -> str:
    path = Path(__file__).parent / "prompts" / "form_agent_system.md"
    if not path.exists():
        return "You are a job application agent."
    return path.read_text(encoding="utf-8")


def _ats_specific_hints(platform: str) -> str:
    """Provide platform-specific guidance for the agent."""
    hints = {
        "workday": [
            "## Workday-specific guidance",
            "- This is a multi-step wizard. Click the Next button (data-automation-id='bottom-navigation-next-button') to advance.",
            "- Dropdowns are CUSTOM — not native <select>. Click the dropdown, wait for the listbox ([role='listbox']), then click the option.",
            "- File upload uses data-automation-id='file-upload-input-ref'.",
            "- Workday may try to parse your resume and pre-fill fields. Verify pre-filled data matches the candidate info above.",
            "- If you see an account creation/sign-in page, report 'stuck'.",
        ],
        "icims": [
            "## iCIMS-specific guidance",
            "- The form may be inside an iframe. If you see very few form fields, look for an iframe and try interacting within it.",
            "- Account creation may be required. If prompted, report 'stuck' if it blocks progress.",
            "- Forms are multi-step. Look for Continue/Next/Save buttons.",
        ],
        "taleo": [
            "## Taleo-specific guidance",
            "- This is an older-style multi-step wizard. Pages load slowly.",
            "- Field IDs are dynamically generated. Use labels, not IDs.",
            "- Sessions expire after ~15 minutes. Work efficiently.",
            "- Account creation is almost always required. Report 'stuck' if prompted.",
        ],
        "ashby": [
            "## Ashby-specific guidance",
            "- Ashby forms are typically single-page and use standard HTML elements.",
            "- They are generally very clean and easy to fill.",
        ],
    }
    block = hints.get(platform.lower(), [])
    return "\n".join(block) if block else ""


def _build_agent_system_prompt(
    profile: CandidateApplicationProfile,
    listing: JobListing,
    resume_path: Path,
    cover_letter_path: Path | None,
    ats_platform: str | None = None,
) -> str:
    """Build a detailed system prompt for the form-filling agent."""
    template = _load_agent_system_template()

    # 1. Candidate Info
    info = [
        f"Name: {profile.personal.first_name} {profile.personal.last_name}",
        f"Email: {profile.personal.email}",
        f"Phone: {profile.personal.phone_formatted}",
        f"LinkedIn: {profile.personal.linkedin_url}",
        f"GitHub: {profile.personal.github_url}",
        f"Location: {profile.address.city}, {profile.address.state}",
        f"Work authorized in US: {'Yes' if profile.authorization.authorized_us else 'No'}",
        f"Requires sponsorship: {'Yes' if profile.authorization.require_sponsorship else 'No'}",
        f"Clearance: {profile.authorization.clearance}",
    ]
    candidate_info = "\n".join(info)

    # 2. Job Details
    job_details = (
        "<job_context>\n"
        f"  <title>{listing.title}</title>\n"
        f"  <company>{listing.company}</company>\n"
        "</job_context>"
    )

    # 3. Files
    files = [f"Resume file path: {resume_path}"]
    if cover_letter_path:
        files.append(f"Cover letter file path: {cover_letter_path}")
    file_info = "\n".join(files)

    # 4. ATS Hints
    hints = _ats_specific_hints(ats_platform or "")

    return _safe_format(
        template,
        candidate_info=candidate_info,
        job_details=job_details,
        file_info=file_info,
        ats_specific_hints=hints,
        page_state="{page_state}",
        goal_description="{goal_description}",
    )


async def fill_application_with_agent(
    listing: JobListing,
    profile: CandidateApplicationProfile,
    resume_path: Path,
    cover_letter_path: Path | None,
    *,
    client: httpx.AsyncClient | None = None,
    page: Page | None = None,
    llm: LLMClient | None = None,
    run_id: str = "",
    dry_run: bool = True,
    **kwargs: Optional[object],
) -> ApplicationAttempt:
    """Fill any job application form using the autonomous LLM agent."""
    if page is None:
        raise ValueError("Page is required for agent-based filler.")
    if llm is None:
        raise ValueError("LLMClient is required for agent-based filler.")

    # 1. Define goal
    goal = AgentGoal(
        instruction=(
            f"Fill out the job application for {listing.title} at {listing.company}. "
            "Use the provided candidate profile. Upload the resume at the appropriate "
            "step. Stop and signal 'done' when you reach the final Submit or Review step."
        ),
        success_signals=["Review your application", "Submit Application", "Application Sent", "Application Received"],
        require_human_approval_before=["submit", "done"],
        max_steps=60, # Increased per architecture doc
    )

    # 2. Initialize controller
    ats_platform = str(kwargs.get("ats_platform", ""))

    # Workday-specific pre-check: detect account wall
    if ats_platform.lower() == "workday":
        from pipelines.job_agent.application.workday_helpers import (
            detect_workday_account_wall,
            verify_workday_prefill,
        )
        if await detect_workday_account_wall(page):
            return ApplicationAttempt(
                dedup_key=listing.dedup_key,
                status="stuck",
                strategy="agent_workday",
                summary="Workday account wall detected. Manual sign-in or account creation required.",
            )
        
        # Verify pre-fill from resume if on a contact info page
        mismatches = await verify_workday_prefill(page, profile)
        if mismatches:
            logger.info("workday.correcting_mismatches", fields=mismatches)
            # The agent will handle corrections as part of its normal loop
            # we just log it for now as per Prompt 14 instructions.

    system_prompt_base = _build_agent_system_prompt(
        profile=profile,
        listing=listing,
        resume_path=resume_path,
        cover_letter_path=cover_letter_path,
        ats_platform=ats_platform,
    )
    
    # Final interpolation of goal_description (page_state stays literal for controller)
    system_prompt = _safe_format(
        system_prompt_base,
        goal_description=goal.instruction,
    )

    interest_patterns = [ats_platform] if ats_platform else None

    controller = WebAgentController(
        page=page,
        llm_client=llm,
        goal=goal,
        system_prompt=system_prompt,
        run_id=run_id,
        frame_interest_patterns=interest_patterns,
    )

    # 3. Run agent
    logger.info("agent_filler.start", url=listing.url, platform=ats_platform)
    try:
        result: AgentResult = await controller.run()
    except Exception as exc:
        logger.error("agent_filler.failed", error=str(exc))
        from pipelines.job_agent.application._debug import capture_application_failure
        screenshot_path = await capture_application_failure(
            page, listing, run_id, f"agent_{ats_platform or 'generic'}",
            "Agent failed with exception", error=str(exc)
        )
        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="error",
            strategy="agent_generic",
            summary=f"Agent failed with exception: {exc}",
            errors=[str(exc)],
            screenshot_path=screenshot_path,
        )

    # 4. Convert AgentResult to ApplicationAttempt
    if result.status == "completed" or result.status == "awaiting_approval":
        # Success or reached review
        # The controller doesn't automatically save the screenshot to the right place for us
        from pipelines.job_agent.application._debug import capture_application_failure
        screenshot_path = await capture_application_failure(
            page, listing, run_id, f"agent_{ats_platform or 'generic'}",
            f"Agent {result.status}", extra={"summary": result.summary}
        )

        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="awaiting_review" if result.status == "awaiting_approval" else "submitted",
            strategy="agent_generic",
            summary=result.summary,
            steps_taken=result.steps_taken,
            screenshot_path=screenshot_path,
        )

    if result.status == "stuck":
        from pipelines.job_agent.application._debug import capture_application_failure
        screenshot_path = await capture_application_failure(
            page, listing, run_id, f"agent_{ats_platform or 'generic'}",
            "Agent stuck", extra={"summary": result.summary}
        )
        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="stuck",
            strategy="agent_generic",
            summary=result.summary,
            steps_taken=result.steps_taken,
            screenshot_path=screenshot_path,
        )

    return ApplicationAttempt(
        dedup_key=listing.dedup_key,
        status="error",
        strategy="agent_generic",
        summary=f"Agent stopped with status: {result.status}. {result.summary}",
        steps_taken=result.steps_taken,
    )

# Register for both generic and Workday (which uses the generic agent as a base)
register_submitter(SubmissionStrategy.AGENT_GENERIC, fill_application_with_agent)
register_submitter(SubmissionStrategy.AGENT_WORKDAY, fill_application_with_agent)
