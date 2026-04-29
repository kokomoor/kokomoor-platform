"""Application-level deduplication to prevent double-applying.

Persists every submitted or pending-review application in a SQLite
store so re-runs of the pipeline don't resubmit the same listing.

The store matches the architecture doc's ``applied_store`` contract:
``is_already_applied`` / ``filter_unapplied`` / ``mark_applied``, with
a simple audit schema. Writes are serialised through a ``threading.Lock``
so concurrent ``asyncio.to_thread`` hops from the same process can't
collide on the shared connection, and reads go through the same lock
to avoid observing a partially-written row. WAL journal mode is enabled
so the SQLite file can tolerate mixed read/write pressure gracefully.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from core.config import get_settings

if TYPE_CHECKING:
    from pipelines.job_agent.models import JobListing


class ApplicationDedupStore:
    """Track and filter already-applied job listings."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        settings = get_settings()
        self._db_path = str(db_path or settings.application_dedup_db_path)
        self._lock = threading.Lock()
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []

        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Return a thread-local SQLite connection, tracked for close()."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
            self._connections.append(conn)
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS applied_applications (
                    dedup_key TEXT PRIMARY KEY,
                    company TEXT NOT NULL,
                    title TEXT NOT NULL,
                    strategy TEXT,
                    submitted_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    artifact_dir TEXT
                )
                """
            )
            conn.commit()

    async def is_applied(self, listing: JobListing) -> bool:
        """Check whether this listing has already been recorded."""

        def _check() -> bool:
            with self._lock:
                conn = self._get_conn()
                cur = conn.execute(
                    "SELECT 1 FROM applied_applications WHERE dedup_key = ?",
                    (listing.dedup_key,),
                )
                return cur.fetchone() is not None

        return await asyncio.to_thread(_check)

    async def mark_applied(
        self,
        listing: JobListing,
        *,
        strategy: str = "",
        status: str = "applied",
        artifact_dir: str = "",
    ) -> None:
        """Record a successful or pending application idempotently."""

        def _mark() -> None:
            with self._lock:
                conn = self._get_conn()
                conn.execute(
                    """
                    INSERT INTO applied_applications (
                        dedup_key, company, title, strategy, submitted_at, status, artifact_dir
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(dedup_key) DO UPDATE SET
                        strategy=excluded.strategy,
                        status=excluded.status,
                        artifact_dir=excluded.artifact_dir
                    """,
                    (
                        listing.dedup_key,
                        listing.company,
                        listing.title,
                        strategy,
                        time.time(),
                        status,
                        artifact_dir,
                    ),
                )
                conn.commit()

        await asyncio.to_thread(_mark)

    async def filter_unapplied(self, listings: list[JobListing]) -> list[JobListing]:
        """Return only the listings that haven't been applied to yet."""
        if not listings:
            return []

        def _filter() -> list[JobListing]:
            with self._lock:
                conn = self._get_conn()
                keys = [li.dedup_key for li in listings]
                placeholders = ",".join("?" for _ in keys)
                cur = conn.execute(
                    f"SELECT dedup_key FROM applied_applications "
                    f"WHERE dedup_key IN ({placeholders})",
                    keys,
                )
                existing = {row[0] for row in cur.fetchall()}
            return [li for li in listings if li.dedup_key not in existing]

        return await asyncio.to_thread(_filter)

    def close(self) -> None:
        """Close every tracked connection, not just the calling thread's."""
        with self._lock:
            for conn in self._connections:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._connections.clear()
            if hasattr(self._local, "conn"):
                del self._local.conn
