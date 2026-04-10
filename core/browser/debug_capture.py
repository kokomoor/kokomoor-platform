"""Failure-capture utilities for browser automation diagnostics.

When enabled, captures contextual artifacts (metadata JSON, screenshot, HTML)
for failures so runs are debuggable without interactive browser UI.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.async_api import Page

logger = structlog.get_logger(__name__)

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _safe_name(raw: str) -> str:
    return _SAFE_NAME_RE.sub("_", raw).strip("_") or "event"


class FailureCapture:
    """Persist diagnostics for browser automation failures.

    The ``source`` parameter on each method is a string identifier for the
    provider or subsystem that encountered the failure (e.g. ``"linkedin"``).
    """

    def __init__(
        self,
        *,
        enabled: bool,
        base_dir: str,
        run_id: str,
        include_html: bool,
    ) -> None:
        from pathlib import Path

        self._enabled = enabled
        self._base_dir = Path(base_dir)
        self._run_id = run_id
        self._include_html = include_html
        self._counter = 0
        if self._enabled:
            self._base_dir.mkdir(parents=True, exist_ok=True)

    def _event_dir(self, source: str, stage: str) -> Path:
        self._counter += 1
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        event_name = f"{self._counter:04d}-{_safe_name(stage)}-{stamp}"
        path = self._base_dir / self._run_id / source / event_name
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def capture_page_failure(
        self,
        *,
        source: str,
        stage: str,
        reason: str,
        page: Page,
        error: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        """Capture page-level artifacts for a failure."""
        if not self._enabled:
            return []
        event_dir = self._event_dir(source, stage)
        artifacts: list[str] = []

        metadata: dict[str, Any] = {
            "run_id": self._run_id,
            "source": source,
            "stage": stage,
            "reason": reason,
            "error": error or "",
            "captured_at_utc": datetime.now(UTC).isoformat(),
            "extra": extra or {},
        }
        try:
            metadata["page_url"] = page.url
        except Exception:
            metadata["page_url"] = ""
        try:
            metadata["page_title"] = await page.title()
        except Exception:
            metadata["page_title"] = ""

        metadata_path = event_dir / "metadata.json"
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        artifacts.append(str(metadata_path))

        screenshot_path = event_dir / "page.png"
        try:
            await page.screenshot(path=str(screenshot_path), full_page=True)
            artifacts.append(str(screenshot_path))
        except Exception as exc:
            logger.warning(
                "failure_capture.screenshot_failed",
                source=source,
                stage=stage,
                error=str(exc)[:200],
            )

        if self._include_html:
            html_path = event_dir / "page.html"
            try:
                html = await page.content()
                html_path.write_text(html, encoding="utf-8")
                artifacts.append(str(html_path))
            except Exception as exc:
                logger.warning(
                    "failure_capture.html_failed",
                    source=source,
                    stage=stage,
                    error=str(exc)[:200],
                )

        logger.info(
            "failure_capture.saved",
            source=source,
            stage=stage,
            artifacts=artifacts,
        )
        return artifacts

    def capture_metadata_failure(
        self,
        *,
        source: str,
        stage: str,
        reason: str,
        error: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        """Capture non-page metadata for failures before page exists."""
        if not self._enabled:
            return []
        event_dir = self._event_dir(source, stage)
        metadata = {
            "run_id": self._run_id,
            "source": source,
            "stage": stage,
            "reason": reason,
            "error": error or "",
            "captured_at_utc": datetime.now(UTC).isoformat(),
            "extra": extra or {},
        }
        metadata_path = event_dir / "metadata.json"
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        logger.info(
            "failure_capture.saved",
            source=source,
            stage=stage,
            artifacts=[str(metadata_path)],
        )
        return [str(metadata_path)]
