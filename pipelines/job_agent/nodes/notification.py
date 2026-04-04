"""Notification node — send pipeline run summary.

Compiles a digest of the pipeline run (new discoveries, listings
pending review, applications submitted) and sends it via email.
"""

from __future__ import annotations

import structlog

from pipelines.job_agent.state import JobAgentState, PipelinePhase

logger = structlog.get_logger(__name__)


async def notification_node(state: JobAgentState) -> JobAgentState:
    """Send a notification digest for the pipeline run.

    Stub implementation for Milestone 1. Will use
    ``core.notifications.send_notification`` in Milestone 4.

    Args:
        state: Final pipeline state.

    Returns:
        State with phase set to COMPLETE.
    """
    state.phase = PipelinePhase.COMPLETE

    logger.info(
        "pipeline_complete",
        discovered=len(state.discovered_listings),
        qualified=len(state.qualified_listings),
        applied=len(state.applied_listings),
        errors=len(state.errors),
    )

    # TODO: Milestone 4 — send email digest via core.notifications.
    return state
