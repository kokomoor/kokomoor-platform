"""Entry point for the job application agent pipeline.

Run with: python -m pipelines.job_agent

Initialises logging, builds the LangGraph, and executes a pipeline run.
"""

from __future__ import annotations

import asyncio
import sys
from typing import cast

import structlog

from core.config import get_settings
from core.observability import setup_logging
from pipelines.job_agent.graph import build_graph
from pipelines.job_agent.models import JobSource, SearchCriteria
from pipelines.job_agent.state import JobAgentState


async def main() -> None:
    """Execute a single pipeline run."""
    setup_logging()
    logger = structlog.get_logger("job_agent")
    settings = get_settings()

    logger.info(
        "pipeline_init",
        environment=settings.environment.value,
        has_api_key=settings.has_anthropic_key,
    )

    # Build search criteria from config / defaults.
    criteria = SearchCriteria(
        keywords=["technical product manager", "senior software engineer"],
        target_companies=[
            "Anduril",
            "NVIDIA",
            "Commonwealth Fusion Systems",
            "Anthropic",
            "SpaceX",
            "Apple",
        ],
        target_roles=["TPM", "Senior Engineer", "Engineering Manager"],
        salary_floor=170_000,
        sources=[JobSource.WELLFOUND, JobSource.BUILTIN],
    )

    initial_state = JobAgentState(
        search_criteria=criteria,
        run_id="manual-run",
    )

    # Build and invoke the graph.
    graph = build_graph()
    logger.info("pipeline_start", run_id=initial_state.run_id)

    try:
        final_state = cast("JobAgentState", await graph.ainvoke(initial_state))
        logger.info(
            "pipeline_finished",
            phase=final_state.phase.value,
            discovered=len(final_state.discovered_listings),
            qualified=len(final_state.qualified_listings),
            errors=len(final_state.errors),
        )
    except Exception:
        logger.exception("pipeline_crashed")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
