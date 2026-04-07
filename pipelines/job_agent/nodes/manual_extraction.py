"""Manual extraction node: URL -> standardized ``JobListing``.

This node supports a truncated pipeline where a human provides a direct
job URL and the pipeline starts from extraction instead of discovery/filtering.
"""

from __future__ import annotations

import json

import structlog

from pipelines.job_agent.extraction import extract_job_data_from_url, generate_dedup_key
from pipelines.job_agent.models import ApplicationStatus, JobListing
from pipelines.job_agent.state import JobAgentState, PipelinePhase

logger = structlog.get_logger(__name__)


async def manual_extraction_node(state: JobAgentState) -> JobAgentState:
    """Extract one manual job URL and populate ``qualified_listings``."""
    state.phase = PipelinePhase.DISCOVERY
    state.discovered_listings = []
    state.qualified_listings = []

    if not state.manual_job_url:
        state.errors.append(
            {
                "node": "manual_extraction",
                "message": "manual_job_url is required",
            }
        )
        logger.warning("manual_extract.missing_url")
        return state

    try:
        extracted = await extract_job_data_from_url(state.manual_job_url)
        title = extracted.title or "Unknown Role"
        company = extracted.company or "Unknown Company"
        dedup_key = generate_dedup_key(company, title, extracted.canonical_url)

        listing = JobListing(
            title=title,
            company=company,
            location=extracted.location,
            url=extracted.canonical_url,
            source=extracted.source,
            description=extracted.normalized_description or extracted.raw_description,
            salary_min=extracted.salary_min,
            salary_max=extracted.salary_max,
            remote=extracted.remote,
            status=ApplicationStatus.DISCOVERED,
            dedup_key=dedup_key,
            notes=json.dumps(
                {
                    "employment_type": extracted.employment_type,
                    "role_summary": extracted.role_summary,
                    **extracted.metadata,
                },
                sort_keys=True,
            ),
        )
        state.discovered_listings = [listing]
        state.qualified_listings = [listing]
        logger.info(
            "manual_extract.complete",
            url=extracted.canonical_url,
            source=extracted.source.value,
            chars=len(listing.description),
            salary_min=listing.salary_min,
            salary_max=listing.salary_max,
        )
    except Exception as exc:
        state.errors.append(
            {
                "node": "manual_extraction",
                "url": state.manual_job_url,
                "message": str(exc)[:500],
            }
        )
        logger.warning("manual_extract.failed", url=state.manual_job_url, error=str(exc)[:200])

    return state
