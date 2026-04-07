"""Typed configuration management using Pydantic Settings.

Loads configuration from environment variables and .env files. All config
is validated at startup — if a required value is missing or malformed,
the application fails fast with a clear error rather than silently using
defaults that cause mysterious failures later.

Usage:
    from core.config import get_settings
    settings = get_settings()
    print(settings.database_url)
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve project root relative to this file's location.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Environment(StrEnum):
    """Deployment environment."""

    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class Settings(BaseSettings):
    """Platform-wide configuration.

    Values are loaded from environment variables (prefixed ``KP_``) and
    a ``.env`` file at the project root.  Secret values use ``SecretStr``
    so they are never accidentally logged or serialised.
    """

    model_config = SettingsConfigDict(
        env_prefix="KP_",
        env_file=str(_PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Environment ---
    environment: Environment = Environment.DEV

    # --- Database ---
    database_url: str = Field(
        default=f"sqlite+aiosqlite:///{_PROJECT_ROOT / 'data' / 'platform.db'}",
        description="SQLAlchemy-style connection string. Default: local SQLite.",
    )

    # --- LLM (Anthropic) ---
    anthropic_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Anthropic API key. Required for any LLM operations.",
    )
    anthropic_model: str = Field(
        default="claude-sonnet-4-20250514",
        description="Default Claude model for API calls.",
    )
    anthropic_max_retries: int = Field(default=3, ge=1, le=10)
    anthropic_timeout_seconds: int = Field(default=120, ge=10)

    # --- Browser (Playwright) ---
    browser_headless: bool = Field(
        default=True,
        description="Run Playwright browsers in headless mode.",
    )
    browser_rate_limit_seconds: float = Field(
        default=5.0,
        ge=1.0,
        description="Minimum seconds between page navigations.",
    )

    # --- HTTP / fetch (shared across pipelines) ---
    fetch_http_timeout_seconds: float = Field(
        default=20.0,
        ge=5.0,
        le=300.0,
        description="Timeout for httpx fetches in core.fetch.HttpFetcher.",
    )
    fetch_http_max_retries: int = Field(
        default=2,
        ge=0,
        le=10,
        description="Retry count for failed HTTP fetches (after first attempt).",
    )
    fetch_browser_post_wait_ms: int = Field(
        default=1500,
        ge=0,
        le=30_000,
        description="Milliseconds to wait after navigation before reading page HTML (browser fetch).",
    )

    # --- Observability ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = Field(
        default=True,
        description="Emit structured JSON logs (True) or human-readable (False).",
    )
    langsmith_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="LangSmith API key for LLM tracing. Optional.",
    )
    langsmith_project: str = Field(
        default="kokomoor-platform",
        description="LangSmith project name for trace grouping.",
    )

    # --- Notifications ---
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = Field(default=SecretStr(""))
    notification_from_email: str = ""
    notification_to_email: str = ""

    # --- Job Analysis Node ---
    job_analysis_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Model for the job-analysis node (structured JD extraction).",
    )
    job_analysis_max_tokens: int = Field(
        default=2048,
        ge=256,
        description="Max output tokens for the job-analysis LLM call.",
    )
    job_analysis_max_input_chars: int = Field(
        default=30_000,
        ge=2000,
        description="Safety cap on JD character length sent to job-analysis LLM.",
    )
    job_analysis_enable_cache: bool = Field(
        default=True,
        description="Cache job-analysis results in memory by dedup_key within a run.",
    )

    # --- Resume Tailoring ---
    resume_master_profile_path: str = Field(
        default=str(
            _PROJECT_ROOT / "pipelines" / "job_agent" / "context" / "candidate_profile.yaml"
        ),
        description="Path to the master resume profile YAML.",
    )
    resume_output_dir: str = Field(
        default=str(_PROJECT_ROOT / "data" / "tailored_resumes"),
        description="Directory for generated tailored resume .docx files.",
    )
    resume_enable_critique: bool = Field(
        default=False,
        description="Enable optional LLM critique pass after tailoring.",
    )
    resume_plan_model: str = Field(
        default="",
        description="Model for the tailoring-plan pass. Empty = use default anthropic_model.",
    )
    resume_plan_max_tokens: int = Field(
        default=2048,
        ge=512,
        description="Max output tokens for the tailoring-plan LLM call.",
    )

    # --- Feature Flags ---
    enable_browser_stealth: bool = Field(
        default=True,
        description="Enable anti-detection measures in Playwright.",
    )

    @property
    def is_dev(self) -> bool:
        """Check if running in development mode."""
        return self.environment == Environment.DEV

    @property
    def has_anthropic_key(self) -> bool:
        """Check if an Anthropic API key is configured."""
        return bool(self.anthropic_api_key.get_secret_value())

    @property
    def has_langsmith_key(self) -> bool:
        """Check if LangSmith tracing is configured."""
        return bool(self.langsmith_api_key.get_secret_value())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance.

    Cached so that repeated calls don't re-parse the environment.
    Call ``get_settings.cache_clear()`` in tests to reset.
    """
    return Settings()
