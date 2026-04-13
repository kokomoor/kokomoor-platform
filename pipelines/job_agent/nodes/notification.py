"""Notification node — send pipeline run summary.

Compiles a digest of the pipeline run (new discoveries, listings
pending review, applications submitted) and sends it via email.
"""

from __future__ import annotations

from collections import defaultdict
import structlog

from pipelines.job_agent.state import JobAgentState, PipelinePhase
from core.notifications import send_notification

logger = structlog.get_logger(__name__)


async def notification_node(state: JobAgentState) -> JobAgentState:
    """Send a notification digest for the pipeline run.

    Groups application results and sends a summary block.

    Args:
        state: Final pipeline state.

    Returns:
        State with phase set to COMPLETE.
    """
    state.phase = PipelinePhase.COMPLETE

    tailored_with_resume = sum(
        1 for li in state.tailored_listings if li.tailored_resume_path is not None
    )

    # Group application attempts by status
    by_status = defaultdict(list)
    for attempt in state.application_results:
        by_status[attempt.status].append(attempt)

    submitted_count = len(by_status["submitted"])
    awaiting_count = len(by_status["awaiting_review"])
    stuck_count = len(by_status["stuck"])
    error_count = len(by_status["error"])

    logger.info(
        "pipeline_complete",
        discovered=len(state.discovered_listings),
        qualified=len(state.qualified_listings),
        tailored=tailored_with_resume,
        submitted=submitted_count,
        awaiting_review=awaiting_count,
        stuck=stuck_count,
        application_errors=error_count,
        pipeline_errors=len(state.errors),
    )

    # Build the email summary
    body_lines = [
        f"Pipeline Run Completed",
        f"Discovered: {len(state.discovered_listings)}",
        f"Qualified: {len(state.qualified_listings)}",
        f"Tailored: {tailored_with_resume}",
        f"Submitted Applications: {submitted_count}",
        f"Awaiting Review: {awaiting_count}",
        f"Stuck/Manual Intervention Required: {stuck_count}",
        f"Application Errors: {error_count}",
        "",
        "--- Application Summary ---",
    ]

    if state.application_results:
        for status, attempts in by_status.items():
            body_lines.append(f"\n[{status.upper()}] ({len(attempts)}):")
            for attempt in attempts:
                title = attempt.title if hasattr(attempt, "title") else attempt.dedup_key
                body_lines.append(f"  - {attempt.dedup_key}: {attempt.summary}")
    else:
        body_lines.append("No applications attempted in this run.")

    body_text = "\n".join(body_lines)

    try:
        await send_notification(
            subject=f"Kokomoor Pipeline Summary: {submitted_count} submitted, {awaiting_count} awaiting",
            body=body_text,
        )
        logger.info("notification_sent", subject=f"Kokomoor Pipeline Summary: {submitted_count} submitted, {awaiting_count} awaiting")
    except Exception as exc:
        logger.error("notification_failed", error=str(exc))

    return state