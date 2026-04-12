"""Deduplication for discovered listing refs.

Three phases:
1. In-run: a set of ``dedup_key`` values already seen this run.
2. Database: bulk SELECT against ``job_listings`` to skip listings that
   were tracked on a prior run.
3. File fallback: a JSON store used only when the database is genuinely
   unreachable (not merely missing its schema — that is a real error).

Order matters: in-run dedup happens first (cheap), DB check second, file
fallback third. The database is the source of truth; the file store
exists for environments where no database is configured yet.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.exc import OperationalError
from sqlmodel import col, select

from core.database import get_session
from pipelines.job_agent.discovery.dedup_store import FileDedup
from pipelines.job_agent.models import JobListing

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
    use_file_fallback: bool = True,
) -> list[ListingRef]:
    """Remove listings already seen this run or persisted in the database.

    Args:
        refs: Newly discovered listing refs from one provider batch.
        in_run_seen: Mutable set of dedup keys already emitted this run.
        check_db: Whether to consult ``job_listings`` (default True).
        use_file_fallback: Whether to consult the JSON file store when the
            database is genuinely unreachable. Set False in production
            deployments where the DB is authoritative and a connection
            error should fail loudly instead of silently de-duping from
            a stale file.
    """
    total_input = len(refs)

    # Phase 1: in-run dedup
    phase1: list[tuple[str, ListingRef]] = []
    for ref in refs:
        key = compute_dedup_key(ref.company, ref.title, ref.url)
        if key not in in_run_seen:
            in_run_seen.add(key)
            phase1.append((key, ref))

    after_in_run = len(phase1)

    # Phase 2: database dedup. A missing ``job_listings`` table is a
    # schema-bootstrap bug (init_db / alembic upgrade not run), not an
    # expected path — we log at error level so it cannot hide behind the
    # warning-level noise produced by transient network errors.
    db_ok = False
    if check_db and phase1:
        keys_to_check = [key for key, _ in phase1]
        try:
            async with get_session() as session:
                result = await session.execute(
                    select(JobListing.dedup_key).where(col(JobListing.dedup_key).in_(keys_to_check))
                )
                existing_keys: set[str] = set(result.scalars().all())
            phase1 = [(k, r) for k, r in phase1 if k not in existing_keys]
            db_ok = True
        except OperationalError as exc:
            logger.error(
                "dedup_db_schema_missing",
                hint="run `alembic upgrade head` or call core.database.init_db()",
                error=str(exc)[:200],
            )
        except Exception as exc:
            logger.warning(
                "dedup_db_unreachable",
                refs_considered=len(phase1),
                error=str(exc)[:200],
            )

    # Phase 3: file-based fallback, only used when explicitly enabled and
    # the DB check did not succeed.
    file_dedup: FileDedup | None = None
    if use_file_fallback:
        file_dedup = FileDedup()
        if not db_ok and phase1:
            file_existing = file_dedup.contains_batch([k for k, _ in phase1])
            if file_existing:
                logger.info(
                    "dedup_file_removed",
                    removed=len(file_existing),
                )
                phase1 = [(k, r) for k, r in phase1 if k not in file_existing]

    after_db = len(phase1)
    final = [ref for _, ref in phase1]

    # Persist new keys to the file store for future runs (belt-and-braces
    # even when the DB succeeded — costs nothing and protects against the
    # database file being wiped between runs).
    new_keys = [k for k, _ in phase1]
    if file_dedup is not None and new_keys:
        file_dedup.add_batch(new_keys)
        file_dedup.save()

    logger.info(
        "dedup_complete",
        total_input=total_input,
        after_in_run=after_in_run,
        after_db=after_db,
        final=len(final),
        db_ok=db_ok,
        file_store_size=file_dedup.size if file_dedup is not None else 0,
    )
    return final
