"""Tracking node — persist listing state to the database.

Writes or updates job listing records in SQLite so the pipeline
maintains durable state across runs. Enables deduplication and
status tracking over time.
"""

from __future__ import annotations

import structlog

from pipelines.job_agent.state import JobAgentState, PipelinePhase

logger = structlog.get_logger(__name__)


async def tracking_node(state: JobAgentState) -> JobAgentState:
    """Persist current listing states to the database.

    Stub implementation for Milestone 1. Will use ``core.database``
    sessions to upsert ``JobListing`` records in Milestone 2+.

    Args:
        state: Current pipeline state.

    Returns:
        Unchanged state (persistence is a side effect).
    """
    state.phase = PipelinePhase.TRACKING

    total = (
        len(state.discovered_listings)
        + len(state.qualified_listings)
        + len(state.applied_listings)
    )

    logger.info(
        "tracking_update",
        total=total,
        discovered=len(state.discovered_listings),
        qualified=len(state.qualified_listings),
        applied=len(state.applied_listings),
    )

    # TODO: Milestone 2 — upsert listings into SQLite via core.database.
    return state
