"""Async notification helpers (SMTP + optional IMAP watcher).

Provides a simple interface for sending notification emails (human review
digests, error alerts, etc.) from any pipeline. IMAP reply watching is
implemented in ``core.notifications.inbox``.

Usage:
    from core.notifications import send_notification

    await send_notification(
        subject="3 applications ready for review",
        body="...",
    )
"""

from __future__ import annotations

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosmtplib
import structlog

from core.config import get_settings
from core.notifications.inbox import InboxWatcher as InboxWatcher

logger = structlog.get_logger(__name__)


async def send_notification(
    subject: str,
    body: str,
    *,
    to_email: str | None = None,
    html: bool = False,
) -> bool:
    """Send an email notification.

    Args:
        subject: Email subject line.
        body: Email body (plain text or HTML).
        to_email: Recipient override. Defaults to settings.notification_to_email.
        html: If True, send as HTML email.

    Returns:
        True if sent successfully, False otherwise.
    """
    settings = get_settings()
    recipient = to_email or settings.notification_to_email

    if not settings.smtp_host or not recipient:
        logger.warning("notification_skipped", reason="SMTP not configured")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.notification_from_email
    msg["To"] = recipient

    content_type = "html" if html else "plain"
    msg.attach(MIMEText(body, content_type))

    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password.get_secret_value(),
            use_tls=True,
        )
        logger.info("notification_sent", subject=subject, to=recipient)
        return True
    except Exception:
        logger.exception("notification_failed", subject=subject)
        return False
