from __future__ import annotations

import time as time_mod
from typing import Any

import pytest

from core.config import get_settings
from core.notifications.heal_auth import build_heal_trigger_token, verify_heal_trigger_token


def test_heal_token_round_trip(monkeypatch: Any) -> None:
    monkeypatch.setenv("KP_HEAL_TRIGGER_SIGNING_SECRET", "test-secret")
    monkeypatch.setenv("KP_HEAL_TRIGGER_TOKEN_TTL_S", "3600")
    get_settings.cache_clear()

    token = build_heal_trigger_token("abc123")
    assert verify_heal_trigger_token(token, expected_heal_id="abc123")

    get_settings.cache_clear()


def test_heal_token_rejects_wrong_heal_id(monkeypatch: Any) -> None:
    monkeypatch.setenv("KP_HEAL_TRIGGER_SIGNING_SECRET", "test-secret")
    get_settings.cache_clear()

    token = build_heal_trigger_token("abc123")
    assert not verify_heal_trigger_token(token, expected_heal_id="other")

    get_settings.cache_clear()


def test_heal_token_rejects_expired(monkeypatch: Any) -> None:
    monkeypatch.setenv("KP_HEAL_TRIGGER_SIGNING_SECRET", "test-secret")
    monkeypatch.setenv("KP_HEAL_TRIGGER_TOKEN_TTL_S", "60")
    get_settings.cache_clear()

    now = time_mod.time()
    monkeypatch.setattr(time_mod, "time", lambda: now)
    token = build_heal_trigger_token("abc123")

    monkeypatch.setattr(time_mod, "time", lambda: now + 120)
    assert not verify_heal_trigger_token(token, expected_heal_id="abc123")

    get_settings.cache_clear()


def test_heal_token_build_requires_secret(monkeypatch: Any) -> None:
    monkeypatch.setenv("KP_HEAL_TRIGGER_SIGNING_SECRET", "")
    get_settings.cache_clear()

    with pytest.raises(ValueError, match="KP_HEAL_TRIGGER_SIGNING_SECRET"):
        build_heal_trigger_token("abc123")

    get_settings.cache_clear()


def test_heal_token_verify_rejects_empty_secret(monkeypatch: Any) -> None:
    monkeypatch.setenv("KP_HEAL_TRIGGER_SIGNING_SECRET", "")
    get_settings.cache_clear()

    assert not verify_heal_trigger_token("anything.123.sig", expected_heal_id="anything")

    get_settings.cache_clear()


def test_heal_token_rejects_malformed(monkeypatch: Any) -> None:
    monkeypatch.setenv("KP_HEAL_TRIGGER_SIGNING_SECRET", "test-secret")
    get_settings.cache_clear()

    assert not verify_heal_trigger_token("", expected_heal_id="x")
    assert not verify_heal_trigger_token("too-few.parts", expected_heal_id="x")
    assert not verify_heal_trigger_token("a.b.c.d", expected_heal_id="x")
    assert not verify_heal_trigger_token("abc.notanumber.sig", expected_heal_id="x")

    get_settings.cache_clear()
