"""Deduplication for discovered listing refs.

Two phases:
1. In-run: a set of dedup_keys already seen this run (passed in, mutated in place).
2. Database: bulk SELECT against job_listings table to skip already-tracked listings.

Order matters: in-run dedup happens first (cheap), DB check second (one query per batch).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pipelines.job_agent.discovery.models import ListingRef

logger = structlog.get_logger(__name__)


def compute_dedup_key(company: str, title: str, url: str) -> str:
    """Generate a deterministic dedup key from listing identifiers."""
    raw = f"{company.lower().strip()}|{title.lower().strip()}|{url.strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


async def deduplicate_refs(
    refs: list[ListingRef],
    *,
    in_run_seen: set[str],
    check_db: bool = True,
) -> list[ListingRef]:
    """Remove listings already seen this run or persisted in the database."""
    total_input = len(refs)

    # Phase 1: in-run dedup
    phase1: list[tuple[str, ListingRef]] = []
    for ref in refs:
        key = compute_dedup_key(ref.company, ref.title, ref.url)
        if key not in in_run_seen:
            in_run_seen.add(key)
            phase1.append((key, ref))

    after_in_run = len(phase1)

    # Phase 2: database dedup
    if check_db and phase1:
        from sqlmodel import col, select

        from core.database import get_session
        from pipelines.job_agent.models import JobListing

        keys_to_check = [key for key, _ in phase1]
        async with get_session() as session:
            result = await session.execute(
                select(JobListing.dedup_key).where(col(JobListing.dedup_key).in_(keys_to_check))
            )
            existing_keys: set[str] = set(result.scalars().all())

        phase1 = [(k, r) for k, r in phase1 if k not in existing_keys]

    after_db = len(phase1)
    final = [ref for _, ref in phase1]

    logger.info(
        "dedup_complete",
        total_input=total_input,
        after_in_run=after_in_run,
        after_db=after_db,
        final=len(final),
    )
    return final
