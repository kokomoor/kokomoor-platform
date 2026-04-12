"""LangGraph application node orchestrator.

Prompt 05 scope:
- Fully supports Greenhouse API submitter.
- Routes all other strategies to a structured "stuck" result.
- Wires in run-level limits, profile loading, and listing status updates.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from core.config import Settings, get_settings
from pipelines.job_agent.application.models import ApplicationAttempt
from pipelines.job_agent.application.router import SubmissionStrategy, route_application
from pipelines.job_agent.application.submitters.greenhouse_api import submit_greenhouse_application
from pipelines.job_agent.models import ApplicationStatus, load_application_profile
from pipelines.job_agent.state import JobAgentState, PipelinePhase

if TYPE_CHECKING:
    from core.llm.protocol import LLMClient
    from pipelines.job_agent.models import CandidateApplicationProfile, JobListing

logger = structlog.get_logger(__name__)


async def application_node(
    state: JobAgentState,
    *,
    llm_client: LLMClient | None = None,
) -> JobAgentState:
    """Attempt applications for listings that reached the tailoring phase."""
    state.phase = PipelinePhase.APPLICATION
    settings = get_settings()
    log = logger.bind(run_id=state.run_id)

    if not state.tailored_listings:
        log.info("application.no_tailored_listings")
        state.application_results = []
        return state

    profile = load_application_profile(Path(settings.candidate_application_profile_path))
    processable = [li for li in state.tailored_listings if li.status != ApplicationStatus.ERRORED]

    max_per_run = settings.application_max_per_run
    if max_per_run > 0:
        processable = processable[:max_per_run]

    attempts: list[ApplicationAttempt] = []
    total = len(processable)

    for index, listing in enumerate(processable):
        try:
            attempt = await _apply_to_listing(
                listing=listing,
                profile=profile,
                llm_client=llm_client,
                settings=settings,
                run_id=state.run_id,
                dry_run=state.dry_run,
            )
        except Exception as exc:  # pragma: no cover - defensive guard
            message = str(exc)[:500]
            listing.status = ApplicationStatus.ERRORED
            state.errors.append(
                {
                    "node": "application",
                    "dedup_key": listing.dedup_key,
                    "message": message,
                }
            )
            attempt = ApplicationAttempt(
                dedup_key=listing.dedup_key,
                status="error",
                strategy="application_node",
                summary=message,
                errors=[message],
            )

        attempts.append(attempt)
        _apply_status_transition(state, listing, attempt)

        if index < total - 1 and not state.dry_run:
            await asyncio.sleep(settings.application_min_delay_seconds)

    state.application_results = attempts
    log.info(
        "application.complete",
        attempted=total,
        submitted=sum(1 for a in attempts if a.status == "submitted"),
        awaiting_review=sum(1 for a in attempts if a.status == "awaiting_review"),
        stuck=sum(1 for a in attempts if a.status == "stuck"),
        errors=sum(1 for a in attempts if a.status == "error"),
    )
    return state


async def _apply_to_listing(
    *,
    listing: JobListing,
    profile: CandidateApplicationProfile,
    llm_client: LLMClient | None,
    settings: Settings,
    run_id: str,
    dry_run: bool,
) -> ApplicationAttempt:
    """Apply to one listing using the currently available strategy set."""
    route = await route_application(listing)

    if route.strategy == SubmissionStrategy.API_GREENHOUSE:
        if not listing.tailored_resume_path:
            msg = "Missing tailored resume path for Greenhouse submission."
            raise ValueError(msg)

        resume_path = Path(listing.tailored_resume_path)
        cover_letter_path = (
            Path(listing.tailored_cover_letter_path) if listing.tailored_cover_letter_path else None
        )

        dry_run_mode = dry_run or settings.application_require_human_review
        return await submit_greenhouse_application(
            listing,
            profile,
            resume_path,
            cover_letter_path,
            llm=llm_client,
            run_id=run_id,
            dry_run=dry_run_mode,
        )

    return ApplicationAttempt(
        dedup_key=listing.dedup_key,
        status="stuck",
        strategy=route.strategy.value,
        summary=f"{route.strategy.value} not yet implemented",
        errors=[f"Unimplemented strategy: {route.strategy.value}"],
    )


def _apply_status_transition(
    state: JobAgentState,
    listing: JobListing,
    attempt: ApplicationAttempt,
) -> None:
    """Update listing/application state based on attempt outcome."""
    if attempt.status == "submitted":
        listing.status = ApplicationStatus.APPLIED
        listing.applied_at = datetime.now(UTC)
        return

    if attempt.status == "awaiting_review":
        listing.status = ApplicationStatus.PENDING_REVIEW
        return

    listing.status = ApplicationStatus.ERRORED
    state.errors.append(
        {
            "node": "application",
            "dedup_key": listing.dedup_key,
            "message": attempt.summary[:500],
        }
    )
