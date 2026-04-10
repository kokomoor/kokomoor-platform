"""Tests for SessionStore: load, save, exists, is_fresh, invalidate."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from core.browser.session import SessionStore
from pipelines.job_agent.models import JobSource


@pytest.fixture()
def sessions_dir(tmp_path: Path) -> Path:
    d = tmp_path / "sessions"
    d.mkdir()
    return d


@pytest.fixture()
def store(sessions_dir: Path) -> SessionStore:
    return SessionStore(sessions_dir)


class TestExists:
    def test_missing_returns_false(self, store: SessionStore) -> None:
        assert store.exists(JobSource.LINKEDIN) is False

    def test_present_returns_true(self, store: SessionStore, sessions_dir: Path) -> None:
        (sessions_dir / "linkedin.json").write_text("{}", encoding="utf-8")
        assert store.exists(JobSource.LINKEDIN) is True


class TestIsFresh:
    def test_missing_file_is_not_fresh(self, store: SessionStore) -> None:
        assert store.is_fresh(JobSource.LINKEDIN, max_age_hours=72) is False

    def test_recent_file_is_fresh(self, store: SessionStore, sessions_dir: Path) -> None:
        (sessions_dir / "linkedin.json").write_text("{}", encoding="utf-8")
        assert store.is_fresh(JobSource.LINKEDIN, max_age_hours=72) is True

    def test_zero_max_age_always_stale(self, store: SessionStore, sessions_dir: Path) -> None:
        (sessions_dir / "linkedin.json").write_text("{}", encoding="utf-8")
        assert store.is_fresh(JobSource.LINKEDIN, max_age_hours=0) is False


class TestLoad:
    def test_missing_returns_none(self, store: SessionStore) -> None:
        assert store.load(JobSource.INDEED) is None

    def test_valid_json_returns_dict(self, store: SessionStore, sessions_dir: Path) -> None:
        data = {"cookies": [{"name": "sid", "value": "abc"}]}
        (sessions_dir / "indeed.json").write_text(json.dumps(data), encoding="utf-8")
        result = store.load(JobSource.INDEED)
        assert result == data

    def test_corrupt_json_returns_none(self, store: SessionStore, sessions_dir: Path) -> None:
        (sessions_dir / "indeed.json").write_text("{not valid json!!!", encoding="utf-8")
        assert store.load(JobSource.INDEED) is None


class TestSave:
    @pytest.mark.asyncio
    async def test_writes_storage_state(self, store: SessionStore, sessions_dir: Path) -> None:
        state = {"cookies": [{"name": "token", "value": "xyz"}], "origins": []}
        mock_browser = AsyncMock()
        mock_browser.dump_storage_state = AsyncMock(return_value=state)

        result = await store.save(JobSource.LINKEDIN, mock_browser)

        assert result is True
        path = sessions_dir / "linkedin.json"
        assert path.is_file()
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved == state

    @pytest.mark.asyncio
    async def test_dump_failure_returns_false(self, store: SessionStore) -> None:
        mock_browser = AsyncMock()
        mock_browser.dump_storage_state = AsyncMock(side_effect=RuntimeError("no context"))

        result = await store.save(JobSource.LINKEDIN, mock_browser)
        assert result is False


class TestInvalidate:
    def test_deletes_existing_file(self, store: SessionStore, sessions_dir: Path) -> None:
        path = sessions_dir / "linkedin.json"
        path.write_text("{}", encoding="utf-8")
        store.invalidate(JobSource.LINKEDIN)
        assert not path.is_file()

    def test_missing_file_no_error(self, store: SessionStore) -> None:
        store.invalidate(JobSource.GREENHOUSE)


class TestAgeHours:
    def test_missing_returns_none(self, store: SessionStore) -> None:
        assert store.age_hours(JobSource.LEVER) is None

    def test_existing_returns_float(self, store: SessionStore, sessions_dir: Path) -> None:
        (sessions_dir / "lever.json").write_text("{}", encoding="utf-8")
        age = store.age_hours(JobSource.LEVER)
        assert age is not None
        assert age >= 0.0
