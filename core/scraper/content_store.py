"""Append-only JSONL content store for extracted records.

Persists extracted records in JSONL files partitioned by site and date,
providing a durable, human-inspectable record of everything the scraper
has ever extracted.

Structure::

    data/scraper_content/<site_id>/<YYYY-MM-DD>.jsonl

Each line is a JSON object: ``{"dedup_key": ..., "extracted_at": ..., "data": {...}}``

Old files are compressed to ``.jsonl.gz`` after a configurable retention
period to save disk space while keeping data accessible.
"""

from __future__ import annotations

import gzip
import json
import os
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from core.scraper.path_safety import safe_join, validate_site_id

logger = structlog.get_logger(__name__)

_DEFAULT_BASE_DIR = Path("data/scraper_content")
_DEFAULT_COMPRESS_AFTER_DAYS = 7


class ContentStore:
    """JSONL-based extracted record persistence."""

    def __init__(
        self,
        base_dir: str | Path | None = None,
        *,
        compress_after_days: int = _DEFAULT_COMPRESS_AFTER_DAYS,
    ) -> None:
        self._base_dir = Path(base_dir or _DEFAULT_BASE_DIR)
        self._compress_after_days = compress_after_days
        self._file_locks: dict[Path, threading.Lock] = {}

    def _lock_for(self, path: Path) -> threading.Lock:
        return self._file_locks.setdefault(path, threading.Lock())

    def _site_dir(self, site_id: str) -> Path:
        d = safe_join(self._base_dir, site_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _today_file(self, site_id: str) -> Path:
        return self._site_dir(site_id) / f"{date.today().isoformat()}.jsonl"

    # -- write ---------------------------------------------------------------

    def append(self, site_id: str, records: list[dict[str, Any]]) -> int:
        """Append records to today's JSONL file. Returns count written."""
        if not records:
            return 0
        validate_site_id(site_id)
        path = self._today_file(site_id)
        with self._lock_for(path), path.open("a", encoding="utf-8") as fh:
            for record in records:
                line = json.dumps(record, ensure_ascii=False, default=str)
                fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        logger.debug(
            "content_store.appended",
            site_id=site_id,
            records=len(records),
            path=str(path),
        )
        return len(records)

    def append_with_metadata(
        self,
        site_id: str,
        records: list[dict[str, Any]],
        dedup_keys: list[str],
    ) -> int:
        """Append records wrapped with dedup key and timestamp metadata."""
        validate_site_id(site_id)
        now = datetime.now(UTC).isoformat()
        wrapped = [
            {"dedup_key": dk, "extracted_at": now, "data": rec}
            for dk, rec in zip(dedup_keys, records, strict=True)
        ]
        return self.append(site_id, wrapped)

    # -- read ----------------------------------------------------------------

    def read(
        self,
        site_id: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[dict[str, Any]]:
        """Read records for *site_id* within an optional date range."""
        validate_site_id(site_id)
        site_dir = self._site_dir(site_id)
        results: list[dict[str, Any]] = []

        for path in sorted(site_dir.iterdir()):
            file_date = self._parse_date_from_path(path)
            if file_date is None:
                continue
            if start_date and file_date < start_date:
                continue
            if end_date and file_date > end_date:
                continue
            results.extend(self._read_file(path))

        return results

    def read_latest(self, site_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Read the most recent *limit* records for *site_id*."""
        validate_site_id(site_id)
        site_dir = self._site_dir(site_id)
        all_files = sorted(site_dir.iterdir(), reverse=True)
        results: list[dict[str, Any]] = []

        for path in all_files:
            if self._parse_date_from_path(path) is None:
                continue
            records = self._read_file(path)
            results.extend(reversed(records))
            if len(results) >= limit:
                break

        return list(reversed(results[-limit:]))

    def _read_file(self, path: Path) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        try:
            if path.suffix == ".gz":
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            records.append(json.loads(line))
            elif path.suffix == ".jsonl":
                with path.open(encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            records.append(json.loads(line))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("content_store.read_error", path=str(path), error=str(exc)[:200])
        return records

    @staticmethod
    def _parse_date_from_path(path: Path) -> date | None:
        stem = path.stem
        if stem.endswith(".jsonl"):
            stem = stem[: -len(".jsonl")]
        try:
            return date.fromisoformat(stem)
        except ValueError:
            return None

    # -- stats ---------------------------------------------------------------

    def count(self, site_id: str) -> int:
        """Count total records for *site_id* across all files."""
        validate_site_id(site_id)
        total = 0
        site_dir = self._site_dir(site_id)
        for path in site_dir.iterdir():
            if self._parse_date_from_path(path) is not None:
                total += len(self._read_file(path))
        return total

    def file_count(self, site_id: str) -> int:
        """Count JSONL files for *site_id*."""
        validate_site_id(site_id)
        site_dir = self._site_dir(site_id)
        return sum(1 for p in site_dir.iterdir() if self._parse_date_from_path(p) is not None)

    # -- maintenance ---------------------------------------------------------

    def compress_old(self, site_id: str | None = None) -> int:
        """Compress JSONL files older than retention threshold.

        Returns count of files compressed.
        """
        cutoff = date.today() - timedelta(days=self._compress_after_days)
        compressed = 0

        if site_id:
            dirs = [self._site_dir(validate_site_id(site_id))]
        else:
            if not self._base_dir.exists():
                return 0
            dirs = list(self._base_dir.iterdir())

        for site_dir in dirs:
            if not site_dir.is_dir():
                continue
            for path in sorted(site_dir.iterdir()):
                if path.suffix != ".jsonl":
                    continue
                file_date = self._parse_date_from_path(path)
                if file_date is None or file_date >= cutoff:
                    continue
                gz_path = path.with_suffix(".jsonl.gz")
                tmp_gz = gz_path.with_suffix(gz_path.suffix + ".tmp")
                with self._lock_for(path):
                    if not path.exists():
                        continue
                    with path.open("rb") as f_in, gzip.open(tmp_gz, "wb") as f_out:
                        f_out.writelines(f_in)
                    tmp_gz.replace(gz_path)
                    path.unlink()
                compressed += 1
                logger.debug("content_store.compressed", path=str(gz_path))

        if compressed:
            logger.info("content_store.compress_complete", files=compressed)
        return compressed
