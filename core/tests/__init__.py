"""Tests for core configuration management."""

from __future__ import annotations

import os

from core.config import Environment, Settings, get_settings


class TestSettings:
    """Tests for the Settings model."""

    def test_defaults(self) -> None:
        """Settings loads with sensible defaults."""
        s = Settings()
        assert s.environment == Environment.DEV
        assert s.log_level == "INFO"
        assert s.browser_headless is True
        assert s.anthropic_max_retries == 3

    def test_is_dev(self) -> None:
        """is_dev property reflects environment."""
        s = Settings(environment=Environment.DEV)
        assert s.is_dev is True
        s = Settings(environment=Environment.PROD)
        assert s.is_dev is False

    def test_has_anthropic_key_empty(self) -> None:
        """has_anthropic_key is False when key is empty."""
        s = Settings()
        assert s.has_anthropic_key is False

    def test_env_prefix(self) -> None:
        """Settings reads KP_-prefixed environment variables."""
        os.environ["KP_LOG_LEVEL"] = "DEBUG"
        try:
            s = Settings()
            assert s.log_level == "DEBUG"
        finally:
            del os.environ["KP_LOG_LEVEL"]


class TestGetSettings:
    """Tests for the settings singleton."""

    def test_cached(self) -> None:
        """get_settings returns the same instance on repeated calls."""
        get_settings.cache_clear()
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2
        get_settings.cache_clear()
