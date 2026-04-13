"""Application-level deduplication to prevent double-applying.

Uses a custom SQLite store to track which JobListing dedup_keys have
already been successfully submitted or are currently pending review,
with rich auditing capabilities.
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
        self._db_path = db_path or settings.application_dedup_db_path
        self._lock = threading.Lock()
        
        # Ensure parent directory exists
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a thread-local SQLite connection."""
        if not hasattr(self, "_local"):
            self._local = threading.local()
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

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
        """Check if this listing has already been applied to."""
        def _check():
            conn = self._get_conn()
            cur = conn.execute(
                "SELECT 1 FROM applied_applications WHERE dedup_key = ?",
                (listing.dedup_key,)
            )
            return cur.fetchone() is not None
            
        async with asyncio.Lock(): # Or just rely on thread executor
            return await asyncio.to_thread(_check)

    async def mark_applied(self, listing: JobListing, strategy: str = "", status: str = "applied", artifact_dir: str = "") -> None:
        """Record a successful or pending application."""
        def _mark():
            with self._lock:
                conn = self._get_conn()
                now = time.time()
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
                        now,
                        status,
                        artifact_dir,
                    )
                )
                conn.commit()

        await asyncio.to_thread(_mark)

    async def filter_unapplied(self, listings: list[JobListing]) -> list[JobListing]:
        """Return only the listings that haven't been applied to yet."""
        if not listings:
            return []
            
        def _filter():
            conn = self._get_conn()
            keys = [li.dedup_key for li in listings]
            placeholders = ",".join("?" for _ in keys)
            cur = conn.execute(
                f"SELECT dedup_key FROM applied_applications WHERE dedup_key IN ({placeholders})",
                keys
            )
            existing = {row[0] for row in cur.fetchall()}
            return [li for li in listings if li.dedup_key not in existing]

        return await asyncio.to_thread(_filter)

    async def claim_for_application(self, listing: JobListing) -> bool:
        """Atomic check-and-set to claim a listing for the current run."""
        def _claim():
            with self._lock:
                conn = self._get_conn()
                now = time.time()
                try:
                    conn.execute(
                        """
                        INSERT INTO applied_applications (
                            dedup_key, company, title, strategy, submitted_at, status, artifact_dir
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            listing.dedup_key,
                            listing.company,
                            listing.title,
                            "pending",
                            now,
                            "pending",
                            "",
                        )
                    )
                    conn.commit()
                    return True
                except sqlite3.IntegrityError:
                    return False
                    
        return await asyncio.to_thread(_claim)

    def close(self) -> None:
        if hasattr(self, "_local") and hasattr(self._local, "conn"):
            self._local.conn.close()
            del self._local.conn
