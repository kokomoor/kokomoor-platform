from __future__ import annotations

from typing import Any

from core.config import get_settings
from core.notifications.inbox import InboxWatcher


def test_extract_heal_token_from_body() -> None:
    token = InboxWatcher._extract_heal_token("", "Heal Token: abc.123.sig")
    assert token == "abc.123.sig"


def test_extract_heal_id_from_subject() -> None:
    heal_id = InboxWatcher._extract_heal_id("Re: Heal ID: deadbeef1234", "")
    assert heal_id == "deadbeef1234"


def test_extract_heal_token_returns_none_for_no_match() -> None:
    assert InboxWatcher._extract_heal_token("No token here", "Also nothing") is None


def test_extract_heal_id_returns_none_for_no_match() -> None:
    assert InboxWatcher._extract_heal_id("Normal subject", "Normal body") is None


def test_allowed_senders_parsed_and_lowercased(monkeypatch: Any) -> None:
    monkeypatch.setenv("KP_HEAL_REPLY_ALLOWED_SENDERS", "Alice@Example.COM, bob@test.org ")
    monkeypatch.setenv("KP_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("KP_IMAP_USERNAME", "user")
    monkeypatch.setenv("KP_IMAP_PASSWORD", "pass")
    get_settings.cache_clear()

    async def noop(_heal_id: str) -> None:
        pass

    watcher = InboxWatcher(callback=noop)
    assert watcher._allowed_senders == {"alice@example.com", "bob@test.org"}

    get_settings.cache_clear()


def test_empty_allowed_senders_permits_all(monkeypatch: Any) -> None:
    monkeypatch.setenv("KP_HEAL_REPLY_ALLOWED_SENDERS", "")
    monkeypatch.setenv("KP_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("KP_IMAP_USERNAME", "user")
    monkeypatch.setenv("KP_IMAP_PASSWORD", "pass")
    get_settings.cache_clear()

    async def noop(_heal_id: str) -> None:
        pass

    watcher = InboxWatcher(callback=noop)
    assert watcher._allowed_senders == set()

    get_settings.cache_clear()
