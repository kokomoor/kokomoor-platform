"""File-based deduplication persistence for cross-run dedup.

The primary dedup mechanism is the database (job_listings.dedup_key).
However, the DB requires migrations to be run.  This module provides a
zero-setup fallback: a simple JSON file that tracks seen dedup keys with
timestamps.  It is used automatically when the DB check fails.

Keys older than ``max_age_days`` are pruned on every load to prevent
unbounded growth.

File: data/dedup_seen.json (gitignored via data/ pattern)
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_PATH = Path("data/dedup_seen.json")
_DEFAULT_MAX_AGE_DAYS = 30


class FileDedup:
    """Lightweight file-based dedup key store."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        max_age_days: int = _DEFAULT_MAX_AGE_DAYS,
    ) -> None:
        self._path = path or _DEFAULT_PATH
        self._max_age_days = max_age_days
        self._store: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            self._store = {}
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                self._store = {}
                return
            cutoff = time.time() - (self._max_age_days * 86_400)
            self._store = {
                k: ts for k, ts in raw.items() if isinstance(ts, (int, float)) and ts > cutoff
            }
        except (json.JSONDecodeError, OSError):
            logger.warning("file_dedup.load_failed", path=str(self._path))
            self._store = {}

    def contains(self, key: str) -> bool:
        return key in self._store

    def contains_batch(self, keys: list[str]) -> set[str]:
        return {k for k in keys if k in self._store}

    def add(self, key: str) -> None:
        self._store[key] = time.time()

    def add_batch(self, keys: list[str]) -> None:
        now = time.time()
        for k in keys:
            self._store[k] = now

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._store, indent=None, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.debug("file_dedup.saved", keys=len(self._store), path=str(self._path))
        except OSError:
            logger.warning("file_dedup.save_failed", path=str(self._path), exc_info=True)

    @property
    def size(self) -> int:
        return len(self._store)
