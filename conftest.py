"""Root pytest configuration.

Ensures that core.config uses test defaults and that the settings
cache is cleared between test modules.
"""

from __future__ import annotations

import os

import pytest

# Force test-safe defaults before any Settings are loaded.
os.environ.setdefault("KP_ENVIRONMENT", "dev")
os.environ.setdefault("KP_LOG_JSON", "false")
os.environ.setdefault("KP_LOG_LEVEL", "DEBUG")
os.environ.setdefault("KP_BROWSER_HEADLESS", "true")


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """Clear the settings cache before each test."""
    from core.config import get_settings
    get_settings.cache_clear()
