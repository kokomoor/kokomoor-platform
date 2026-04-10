"""Signing helpers for heal reply trigger tokens."""

from __future__ import annotations

import hashlib
import hmac
import time
from base64 import urlsafe_b64encode

from core.config import get_settings


def _sign(heal_id: str, issued_at: int, secret: str) -> str:
    payload = f"{heal_id}:{issued_at}".encode()
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_heal_trigger_token(heal_id: str) -> str:
    """Create signed token bound to ``heal_id`` and issuance timestamp."""
    settings = get_settings()
    secret = settings.heal_trigger_signing_secret.get_secret_value()
    if not secret:
        msg = "KP_HEAL_TRIGGER_SIGNING_SECRET is required to build heal trigger token."
        raise ValueError(msg)

    issued_at = int(time.time())
    signature = _sign(heal_id, issued_at, secret)
    return f"{heal_id}.{issued_at}.{signature}"


def verify_heal_trigger_token(token: str, *, expected_heal_id: str | None = None) -> bool:
    """Verify signed trigger token authenticity and expiration."""
    settings = get_settings()
    secret = settings.heal_trigger_signing_secret.get_secret_value()
    if not secret:
        return False

    parts = token.split(".")
    if len(parts) != 3:
        return False

    heal_id, issued_raw, signature = parts
    if expected_heal_id and heal_id != expected_heal_id:
        return False

    if not issued_raw.isdigit():
        return False
    issued_at = int(issued_raw)
    if int(time.time()) - issued_at > settings.heal_trigger_token_ttl_s:
        return False

    expected_signature = _sign(heal_id, issued_at, secret)
    return hmac.compare_digest(signature, expected_signature)


def token_payload(token: str) -> tuple[str, int] | None:
    """Return parsed (heal_id, issued_at) for a valid-format token."""
    parts = token.split(".")
    if len(parts) != 3 or not parts[1].isdigit():
        return None
    return parts[0], int(parts[1])
