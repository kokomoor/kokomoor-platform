from __future__ import annotations

import sys
import types
from typing import Any

import pytest

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


def test_explicit_fix_reply_ignores_quoted_history() -> None:
    body = """
    > fix
    > Heal Token: old.token.value
    Looks good, I will review later.
    """
    assert not InboxWatcher._is_explicit_fix_reply(body)


def test_explicit_fix_reply_accepts_first_non_quoted_fix() -> None:
    body = """
    > previous message
    fix
    """
    assert InboxWatcher._is_explicit_fix_reply(body)


@pytest.mark.asyncio
async def test_check_once_rejects_quoted_fix_with_valid_token(monkeypatch: Any) -> None:
    monkeypatch.setenv("KP_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("KP_IMAP_USERNAME", "user")
    monkeypatch.setenv("KP_IMAP_PASSWORD", "pass")
    monkeypatch.setenv("KP_HEAL_TRIGGER_SIGNING_SECRET", "test-secret")
    monkeypatch.setenv("KP_HEAL_REPLY_ALLOWED_SENDERS", "owner@example.com")
    get_settings.cache_clear()

    from core.notifications.heal_auth import build_heal_trigger_token

    heal_id = "abc123"
    token = build_heal_trigger_token(heal_id)
    raw_email = (
        "From: owner@example.com\r\n"
        "Subject: Re: Heal ID: abc123\r\n"
        "Message-ID: <m1@example.com>\r\n"
        "\r\n"
        "> fix\r\n"
        f"> Heal Token: {token}\r\n"
        "Looks good.\r\n"
    ).encode()

    class FakeIMAP:
        async def wait_hello_from_server(self) -> None:
            return None

        async def login(self, _u: str, _p: str) -> None:
            return None

        async def select(self, _mailbox: str) -> None:
            return None

        async def search(self, _query: str) -> tuple[str, list[bytes]]:
            return "OK", [b"1"]

        async def fetch(self, _uid: str, _query: str) -> tuple[str, list[Any]]:
            return "OK", [(b"RFC822", raw_email)]

        async def store(self, _uid: str, _flags: str, _value: str) -> None:
            return None

        async def logout(self) -> None:
            return None

    fake_module = types.SimpleNamespace(IMAP4_SSL=lambda **_: FakeIMAP())
    monkeypatch.setitem(sys.modules, "aioimaplib", fake_module)

    triggered: list[str] = []

    async def callback(heal_id: str) -> None:
        triggered.append(heal_id)

    watcher = InboxWatcher(callback=callback)
    result = await watcher.check_once()
    assert result == []
    assert triggered == []

    get_settings.cache_clear()
