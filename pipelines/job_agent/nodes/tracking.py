"""Tracking node — persist listing state to the database.

Durable state for the job pipeline. Two things happen here:

1. Each listing that survived filtering is upserted into ``job_listings``
   keyed by its unique ``dedup_key``. On conflict we update the mutable
   columns (status, description, tailored-document paths, timestamps)
   so subsequent runs see the latest state without duplicating rows.

2. A ``pipeline_runs`` record is written summarising the run (counts,
   errors, duration metadata). This is the audit trail the dedup
   pipeline and any future dashboard will read from.

The node intentionally tolerates individual upsert failures: a single
bad row must not lose the rest of the batch. Errors are logged onto
``state.errors`` so the notification node can surface them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import select

from core.database import get_session
from core.models import PipelineRun, PipelineRunStatus
from pipelines.job_agent.models import JobListing
from pipelines.job_agent.state import JobAgentState, PipelinePhase

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


# Columns whose values we want to refresh on conflict. Anything not in
# this list (e.g. created_at, id, dedup_key) stays at the original value
# from the first insert so the row's history is preserved.
_UPSERT_REFRESH_COLUMNS: tuple[str, ...] = (
    "title",
    "company",
    "location",
    "url",
    "source",
    "description",
    "salary_min",
    "salary_max",
    "remote",
    "status",
    "tailored_resume_path",
    "tailored_cover_letter_path",
    "notes",
    "updated_at",
)


async def tracking_node(state: JobAgentState) -> JobAgentState:
    """Persist current listing states and the pipeline run to the database.

    Args:
        state: Current pipeline state.

    Returns:
        The state unchanged (persistence is a side effect), with the
        phase marker advanced to ``TRACKING``.
    """
    state.phase = PipelinePhase.TRACKING

    # We upsert qualified listings because every listing that made it
    # past filtering is worth remembering — even if tailoring later
    # errored for it, we still want the record so dedup on the next run
    # does not re-discover it.
    listings_to_persist = state.qualified_listings or state.discovered_listings

    if state.dry_run:
        logger.info("tracking_skip_dry_run", total=len(listings_to_persist))
        return state

    upserted = 0
    try:
        async with get_session() as session:
            for listing in listings_to_persist:
                try:
                    await _upsert_listing(session, listing)
                    upserted += 1
                except Exception as exc:
                    state.errors.append(
                        {
                            "node": "tracking",
                            "dedup_key": listing.dedup_key,
                            "message": f"upsert_failed: {exc}"[:500],
                        }
                    )
                    logger.warning(
                        "tracking_upsert_failed",
                        dedup_key=listing.dedup_key,
                        error=str(exc)[:200],
                    )

            await _write_pipeline_run(session, state, upserted)
            await session.commit()
    except Exception:
        logger.exception("tracking_persist_failed")
        state.errors.append(
            {
                "node": "tracking",
                "dedup_key": "",
                "message": "pipeline run persistence failed; see logs",
            }
        )
        return state

    logger.info(
        "tracking_update",
        total=len(listings_to_persist),
        upserted=upserted,
        discovered=len(state.discovered_listings),
        qualified=len(state.qualified_listings),
        tailored=len(state.tailored_listings),
        application_attempts=len(state.application_results),
        application_submitted=sum(1 for a in state.application_results if a.status == "submitted"),
        application_awaiting_review=sum(
            1 for a in state.application_results if a.status == "awaiting_review"
        ),
    )
    return state


async def _upsert_listing(session: AsyncSession, listing: JobListing) -> None:
    """Insert-or-update a single listing keyed by ``dedup_key``.

    Uses SQLite's native ``ON CONFLICT`` so we emit one round-trip per
    row instead of the read-then-update pattern. Postgres has the same
    construct via ``postgresql.insert`` — swap the import when the
    connection string migrates off sqlite.
    """
    payload = listing.model_dump(exclude={"id"})
    payload.setdefault("created_at", datetime.now(UTC))
    payload["updated_at"] = datetime.now(UTC)

    stmt = sqlite_insert(JobListing).values(**payload)
    refresh = {col: getattr(stmt.excluded, col) for col in _UPSERT_REFRESH_COLUMNS}
    stmt = stmt.on_conflict_do_update(
        index_elements=[JobListing.dedup_key],
        set_=refresh,
    )
    await session.execute(stmt)

    # Reconcile the in-memory row's id with the persisted row so any
    # caller downstream (notification, future application flow) can
    # reference the DB primary key directly.
    if listing.id is None:
        result = await session.execute(
            select(JobListing.id).where(JobListing.dedup_key == listing.dedup_key)
        )
        listing.id = result.scalar_one_or_none()


async def _write_pipeline_run(session: AsyncSession, state: JobAgentState, upserted: int) -> None:
    """Record a ``PipelineRun`` row summarising the execution."""
    metadata = {
        "run_id": state.run_id,
        "discovered": len(state.discovered_listings),
        "qualified": len(state.qualified_listings),
        "tailored": sum(1 for li in state.tailored_listings if li.tailored_resume_path is not None),
        "application_attempts": len(state.application_results),
        "application_submitted": sum(
            1 for attempt in state.application_results if attempt.status == "submitted"
        ),
        "application_awaiting_review": sum(
            1 for attempt in state.application_results if attempt.status == "awaiting_review"
        ),
        "application_stuck": sum(
            1 for attempt in state.application_results if attempt.status == "stuck"
        ),
        "application_errors": sum(
            1 for attempt in state.application_results if attempt.status == "error"
        ),
        "upserted_listings": upserted,
        "error_count": len(state.errors),
    }
    now = datetime.now(UTC)
    run = PipelineRun(
        pipeline_name="job_agent",
        status=PipelineRunStatus.FAILED if state.errors else PipelineRunStatus.COMPLETED,
        started_at=now,
        completed_at=now,
        error_message=_compact_error_summary(state) if state.errors else None,
        metadata_json=json.dumps(metadata, default=str),
    )
    session.add(run)


def _compact_error_summary(state: JobAgentState) -> str:
    """Pack the error list into a single short string for the run row."""
    head = state.errors[:5]
    summary = "; ".join(f"{e.get('node', '?')}:{e.get('message', '')[:80]}" for e in head)
    if len(state.errors) > len(head):
        summary += f" (+{len(state.errors) - len(head)} more)"
    return summary[:2000]
