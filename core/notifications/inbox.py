"""IMAP reply watcher for heal trigger emails.

Monitors the configured IMAP inbox for replies to heal diagnosis emails.
When a user replies with "fix" (case-insensitive), the watcher triggers
the remediation workflow.

Supports two modes:
1. **Periodic poll**: ``watch()`` runs a long-lived loop calling
   ``check_once()`` at a configurable interval.
2. **One-shot**: ``check_once()`` fetches unseen messages once and
   returns immediately.

Usage::

    watcher = InboxWatcher(callback=my_heal_trigger)
    await watcher.watch()       # long-running poll loop
    # or
    await watcher.check_once()  # one-shot poll
"""

from __future__ import annotations

import asyncio
import email
import re
from collections.abc import Awaitable, Callable
from email.utils import parseaddr
from typing import Any

import structlog

from core.config import get_settings
from core.notifications.heal_auth import verify_heal_trigger_token

logger = structlog.get_logger(__name__)

HealCallback = Callable[[str], Awaitable[None]]

_HEAL_ID_PATTERN = re.compile(r"Heal ID:\s*(\w+)")
_HEAL_TOKEN_PATTERN = re.compile(r"Heal Token:\s*([A-Za-z0-9._-]+)")


class InboxWatcher:
    """Watch an IMAP inbox for heal trigger replies."""

    def __init__(
        self,
        callback: HealCallback,
        *,
        imap_host: str = "",
        imap_port: int = 993,
        imap_username: str = "",
        imap_password: str = "",
        poll_interval_s: int = 300,
    ) -> None:
        settings = get_settings()
        self._host = imap_host or settings.imap_host
        self._port = imap_port or settings.imap_port
        self._username = imap_username or settings.imap_username
        self._password = imap_password or settings.imap_password.get_secret_value()
        self._poll_interval = poll_interval_s or settings.heal_reply_poll_interval_s
        self._callback = callback
        self._running = False
        allowed = [
            s.strip().lower() for s in settings.heal_reply_allowed_senders.split(",") if s.strip()
        ]
        self._allowed_senders = set(allowed)
        self._processed_message_ids: set[str] = set()
        self._max_processed_cache = 5000

    async def check_once(self) -> list[str]:
        """One-shot check for heal trigger replies.

        Returns list of heal_ids that were triggered.
        """
        if not self._host or not self._username:
            logger.warning("inbox.not_configured")
            return []

        triggered: list[str] = []

        try:
            import aioimaplib

            imap = aioimaplib.IMAP4_SSL(host=self._host, port=self._port)
            await imap.wait_hello_from_server()
            await imap.login(self._username, self._password)
            await imap.select("INBOX")

            status, data = await imap.search("UNSEEN")
            if status != "OK" or not data or not data[0]:
                await imap.logout()
                return []

            message_ids = data[0].split()

            for msg_id in message_ids:
                uid = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
                fetch_status, msg_data = await imap.fetch(
                    uid,
                    "(RFC822)",
                )
                if fetch_status != "OK" or not msg_data:
                    continue

                raw_email = self._extract_raw_email(msg_data)
                if not raw_email:
                    continue

                msg = email.message_from_bytes(raw_email)
                body = self._get_email_body(msg)
                subject = str(msg.get("Subject", ""))
                sender = parseaddr(str(msg.get("From", "")))[1].lower()
                message_id = str(msg.get("Message-ID", "")).strip() or f"uid:{uid}"

                if message_id in self._processed_message_ids:
                    await self._mark_seen(imap, uid)
                    continue

                if self._allowed_senders and sender not in self._allowed_senders:
                    logger.warning("inbox.unauthorized_sender", sender=sender[:120])
                    await self._mark_seen(imap, uid)
                    continue

                if not self._is_explicit_fix_reply(body):
                    continue

                heal_id = self._extract_heal_id(subject, body)
                token = self._extract_heal_token(subject, body)
                if heal_id and token and verify_heal_trigger_token(token, expected_heal_id=heal_id):
                    triggered.append(heal_id)
                    logger.info(
                        "inbox.heal_trigger_found",
                        heal_id=heal_id,
                        sender=sender[:120],
                    )
                    await self._callback(heal_id)
                    await self._mark_seen(imap, uid)
                    self._processed_message_ids.add(message_id)
                    if len(self._processed_message_ids) > self._max_processed_cache:
                        self._processed_message_ids.clear()
                else:
                    logger.warning(
                        "inbox.invalid_trigger", heal_id=heal_id or "", sender=sender[:120]
                    )
                    await self._mark_seen(imap, uid)

            await imap.logout()

        except ImportError:
            logger.error("inbox.aioimaplib_not_installed")
        except Exception as exc:
            logger.error("inbox.check_failed", error=str(exc)[:300])

        return triggered

    async def watch(self) -> None:
        """Long-running periodic inbox poller.

        Calls ``check_once()`` on a fixed interval. IMAP IDLE push
        notifications are not currently implemented; this is a
        straightforward poll loop.
        """
        self._running = True
        logger.info(
            "inbox.watcher_started",
            host=self._host,
            poll_interval=self._poll_interval,
        )

        while self._running:
            try:
                await self.check_once()
            except Exception as exc:
                logger.error("inbox.watch_error", error=str(exc)[:300])

            await asyncio.sleep(self._poll_interval)

    def stop(self) -> None:
        """Signal the watcher to stop."""
        self._running = False
        logger.info("inbox.watcher_stopped")

    @staticmethod
    def _extract_raw_email(msg_data: Any) -> bytes | None:
        """Extract raw email bytes from IMAP fetch response."""
        if isinstance(msg_data, list):
            for item in msg_data:
                if isinstance(item, tuple) and len(item) >= 2:
                    payload = item[1]
                    if isinstance(payload, bytes):
                        return payload
                elif isinstance(item, bytes) and b"From:" in item:
                    return item
        return None

    @staticmethod
    def _get_email_body(msg: email.message.Message) -> str:
        """Extract plain text body from an email message."""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain":
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        return payload.decode("utf-8", errors="replace")
        else:
            payload = msg.get_payload(decode=True)
            if isinstance(payload, bytes):
                return payload.decode("utf-8", errors="replace")
        return ""

    @staticmethod
    def _extract_heal_id(subject: str, body: str) -> str | None:
        """Extract the heal_id from the email subject or quoted body."""
        for text in [subject, body]:
            match = _HEAL_ID_PATTERN.search(text)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _extract_heal_token(subject: str, body: str) -> str | None:
        """Extract signed heal token from subject/body."""
        for text in [subject, body]:
            match = _HEAL_TOKEN_PATTERN.search(text)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _is_explicit_fix_reply(body: str) -> bool:
        """Accept only direct operator intent, not quoted thread history."""
        for line in body.splitlines():
            trimmed = line.strip()
            if not trimmed:
                continue
            if trimmed.startswith(">"):
                continue
            return trimmed.lower() == "fix"
        return False

    async def _mark_seen(self, imap: Any, uid: str) -> None:
        """Mark a processed message as seen to avoid repeated triggers."""
        try:
            await imap.store(uid, "+FLAGS", "(\\Seen)")
        except Exception as exc:
            logger.warning("inbox.mark_seen_failed", uid=uid, error=str(exc)[:200])
