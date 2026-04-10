"""Bulk extraction node -- fetch full job descriptions for qualified listings.

Runs AFTER filtering (which reduces the listing count) and BEFORE job_analysis
(which needs listing.description to be populated).

For each listing in state.qualified_listings:
- If listing.description is already populated (non-empty), skip it.
- Call extract_job_data_from_url(listing.url) -- the same layered extractor
  used by manual_extraction_node.
- Populate listing.description, listing.title (update if empty),
  listing.company (update if empty), listing.location (update if empty),
  listing.salary_min, listing.salary_max, listing.remote.
- On failure: set listing.status = ERRORED, append to state.errors, continue.

Listings that fail extraction are not removed from qualified_listings -- they
remain with status ERRORED so the job_analysis node skips them gracefully.
"""

from __future__ import annotations

import asyncio
import random

import structlog

from pipelines.job_agent.extraction import extract_job_data_from_url
from pipelines.job_agent.models import ApplicationStatus
from pipelines.job_agent.state import JobAgentState, PipelinePhase

logger = structlog.get_logger(__name__)


async def bulk_extraction_node(state: JobAgentState) -> JobAgentState:
    """Fetch full job page content for each qualified listing."""
    state.phase = PipelinePhase.BULK_EXTRACTION

    if state.dry_run or not state.qualified_listings:
        logger.info(
            "bulk_extraction.skip",
            dry_run=state.dry_run,
            listings=len(state.qualified_listings),
        )
        return state

    success_count = 0
    for listing in state.qualified_listings:
        if listing.description:
            success_count += 1
            continue

        try:
            extracted = await extract_job_data_from_url(listing.url)
            listing.description = extracted.cleaned_description
            if not listing.title or listing.title == "Unknown Role":
                listing.title = extracted.title or listing.title
            if not listing.company or listing.company == "Unknown Company":
                listing.company = extracted.company or listing.company
            if not listing.location:
                listing.location = extracted.location
            if listing.salary_min is None:
                listing.salary_min = extracted.salary_min
            if listing.salary_max is None:
                listing.salary_max = extracted.salary_max
            if listing.remote is None:
                listing.remote = extracted.remote
            listing.status = ApplicationStatus.DISCOVERED
            success_count += 1
            logger.info(
                "bulk_extraction.listing_complete",
                dedup_key=listing.dedup_key,
                description_chars=len(listing.description),
                source=listing.source.value,
            )
            await asyncio.sleep(random.uniform(1.5, 4.0))
        except Exception as exc:
            listing.status = ApplicationStatus.ERRORED
            state.errors.append(
                {
                    "node": "bulk_extraction",
                    "dedup_key": listing.dedup_key,
                    "message": str(exc)[:500],
                }
            )
            logger.warning(
                "bulk_extraction.listing_failed",
                dedup_key=listing.dedup_key,
                error=str(exc)[:200],
            )
            await asyncio.sleep(random.uniform(0.5, 1.5))

    logger.info(
        "bulk_extraction.complete",
        total=len(state.qualified_listings),
        success=success_count,
        errors=len(state.errors),
    )
    return state
