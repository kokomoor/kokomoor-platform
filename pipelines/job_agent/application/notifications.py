"""Application-specific notification helpers.

Provides human-readable summaries for successful, stuck, or failed
application attempts, integrated with the core notification system.
"""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING

from core.notifications import send_notification

if TYPE_CHECKING:
    from pipelines.job_agent.models import JobListing, ApplicationAttempt

logger = structlog.get_logger(__name__)


async def notify_application_status(
    listing: JobListing,
    attempt: ApplicationAttempt,
) -> bool:
    """Send an email notification about an application outcome."""
    status_map = {
        "submitted": "✅ Application Submitted",
        "awaiting_review": "👀 Application Ready for Review",
        "stuck": "🚧 Application Stuck",
        "error": "❌ Application Error",
    }
    
    subject = f"{status_map.get(attempt.status, 'Application Update')}: {listing.company} - {listing.title}"
    
    body = [
        f"Company: {listing.company}",
        f"Position: {listing.title}",
        f"Location: {listing.location}",
        f"URL: {listing.url}",
        "",
        f"Status: {attempt.status.upper()}",
        f"Strategy: {attempt.strategy}",
        f"Summary: {attempt.summary}",
    ]
    
    if attempt.screenshot_path:
        body.append(f"Screenshot: {attempt.screenshot_path}")
        
    if attempt.errors:
        body.append("")
        body.append("Errors:")
        for err in attempt.errors:
            body.append(f"- {err}")

    return await send_notification(
        subject=subject,
        body="\n".join(body),
    )


async def notify_application_batch_summary(
    attempts: list[tuple[JobListing, ApplicationAttempt]],
) -> bool:
    """Send a summary email for a batch of applications."""
    if not attempts:
        return False
        
    submitted = [a for l, a in attempts if a.status == "submitted"]
    review = [a for l, a in attempts if a.status == "awaiting_review"]
    stuck = [a for l, a in attempts if a.status == "stuck"]
    errors = [a for l, a in attempts if a.status == "error"]
    
    subject = f"Job Agent Summary: {len(submitted)} submitted, {len(review)} for review"
    
    body = [
        "Application Run Summary",
        "=======================",
        f"Submitted: {len(submitted)}",
        f"Awaiting Review: {len(review)}",
        f"Stuck: {len(stuck)}",
        f"Errors: {len(errors)}",
        "",
        "Details:",
        "--------"
    ]
    
    for listing, attempt in attempts:
        body.append(f"- [{attempt.status.upper()}] {listing.company}: {listing.title}")
        
    return await send_notification(
        subject=subject,
        body="\n".join(body),
    )
