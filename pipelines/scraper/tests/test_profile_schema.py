"""Tests for strict profile schema validation and legacy key migration."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from pipelines.scraper.models import AuthConfig, NavigationConfig, SiteProfile


def test_navigation_legacy_keys_migrate() -> None:
    cfg = NavigationConfig.model_validate(
        {
            "search_url_template": "https://example.com?q={query}",
            "pagination_strategy": "url_parameter",
            "page_param": "start",
        }
    )
    assert cfg.pagination.value == "url_parameter"
    assert cfg.page_param_name == "start"


def test_auth_legacy_prefix_migrates() -> None:
    cfg = AuthConfig.model_validate(
        {
            "type": "credential",
            "credential_env_prefix": "KP_LINKEDIN",
        }
    )
    assert cfg.type.value == "credential_form"
    assert cfg.env_username_key == "LINKEDIN_EMAIL"
    assert cfg.env_password_key == "LINKEDIN_PASSWORD"


def test_unknown_navigation_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        NavigationConfig.model_validate(
            {
                "search_url_template": "https://example.com?q={query}",
                "unknown_key": "x",
            }
        )


def test_committed_profiles_parse_strictly() -> None:
    profiles_dir = Path("pipelines/scraper/profiles")
    for profile_path in profiles_dir.glob("*.yaml"):
        data = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        profile = SiteProfile.model_validate(data)
        assert profile.site_id
