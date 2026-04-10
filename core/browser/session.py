"""Browser session persistence per provider.

Saves and restores Playwright storage_state (cookies, localStorage, sessionStorage)
between runs. The goal is to maintain established browser sessions that look human
rather than starting fresh each time (fresh contexts trigger bot detection immediately).

Sessions are stored as JSON at: <sessions_dir>/<source_name>.json
This directory is gitignored. Sessions survive between runs and across days.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from pathlib import Path

    from core.browser import BrowserManager

logger = structlog.get_logger(__name__)


class SessionStore:
    """Load / save / invalidate per-source browser sessions.

    The ``source`` parameter on each method is a string identifier
    (e.g. ``"linkedin"``, ``"indeed"``).
    """

    def __init__(self, sessions_dir: Path) -> None:
        self._dir = sessions_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, source: str) -> Path:
        return self._dir / f"{source}.json"

    def exists(self, source: str) -> bool:
        return self._path(source).is_file()

    def age_hours(self, source: str) -> float | None:
        """Return file mtime age in hours, or None if missing."""
        path = self._path(source)
        if not path.is_file():
            return None
        return (time.time() - path.stat().st_mtime) / 3600.0

    def is_fresh(self, source: str, *, max_age_hours: int) -> bool:
        """True if session exists and is younger than max_age_hours."""
        age = self.age_hours(source)
        if age is None:
            return False
        return age < max_age_hours

    def load(self, source: str) -> dict[str, Any] | None:
        """Load a saved session. Returns None on missing or corrupt file."""
        path = self._path(source)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("session_corrupt", source=source, path=str(path))
            return None
        age = self.age_hours(source)
        logger.info(
            "session_load",
            source=source,
            age_hours=round(age, 1) if age is not None else None,
        )
        return data  # type: ignore[no-any-return]

    async def save(self, source: str, browser_manager: BrowserManager) -> bool:
        """Dump browser storage state to disk. Returns True on success."""
        path = self._path(source)
        try:
            state = await browser_manager.dump_storage_state()
            payload = json.dumps(state, indent=2, ensure_ascii=False)
            path.write_text(payload, encoding="utf-8")
            logger.info("session_save", source=source, path=str(path), bytes=len(payload))
        except Exception:
            logger.warning("session_save_failed", source=source, exc_info=True)
            return False
        return True

    def invalidate(self, source: str) -> None:
        """Delete a saved session file."""
        path = self._path(source)
        if path.is_file():
            path.unlink()
        logger.info("session_invalidated", source=source, path=str(path))
