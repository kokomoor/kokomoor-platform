"""Failure-capture wrapper for the application engine.

Provides a unified helper to capture screenshots, HTML, and metadata for
failed or stuck application attempts.
"""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING, Any

from core.browser.debug_capture import FailureCapture
from core.config import get_settings

if TYPE_CHECKING:
    from playwright.async_api import Page
    from pipelines.job_agent.models import JobListing

logger = structlog.get_logger(__name__)


async def capture_application_failure(
    page: Page,
    listing: JobListing,
    run_id: str,
    stage: str,
    reason: str,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Capture a failure bundle and return the screenshot path.

    Args:
        page: The Playwright page where the failure occurred.
        listing: The job listing being applied to.
        run_id: The current pipeline run ID.
        stage: The strategy or stage (e.g. 'agent_workday', 'linkedin_template').
        reason: Human-readable reason for the failure.
        error: Optional technical error message.
        extra: Additional metadata to include in the bundle.
    """
    settings = get_settings()
    capture = FailureCapture(
        enabled=settings.application_debug_capture_enabled,
        base_dir=settings.application_debug_capture_dir,
        run_id=run_id,
        include_html=settings.application_debug_capture_html,
    )

    try:
        artifacts = await capture.capture_page_failure(
            source=listing.dedup_key,
            stage=stage,
            reason=reason,
            page=page,
            error=error,
            extra=extra,
        )
        
        # Look for the screenshot in the bundle
        for artifact in artifacts:
            if artifact.endswith(".png"):
                return artifact
    except OSError as exc:
        logger.error("application_debug.io_failure", error=str(exc))
        raise  # Bubble infrastructure failures
    except Exception as exc:
        logger.warning(
            "application_debug.capture_failed",
            dedup_key=listing.dedup_key,
            error=str(exc),
        )

    return ""
