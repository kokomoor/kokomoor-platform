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
    database_echo: bool = Field(
        default=False,
        description=(
            "Emit raw SQL to stderr (SQLAlchemy ``echo``). Off by default — "
            "the structured pipeline logs are usually more useful and the "
            "echo stream floods them. Enable explicitly when debugging "
            "schema or query issues."
        ),
    )

    # --- LLM (Anthropic) ---
    anthropic_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Anthropic API key. Required for any LLM operations.",
    )
    anthropic_model: str = Field(
        default="claude-sonnet-4-6",
        description=(
            "Default Claude model for API calls. Sonnet 4.6 is the current "
            "recommended general-purpose model (Apr 2026)."
        ),
    )
    anthropic_max_retries: int = Field(default=3, ge=1, le=10)
    anthropic_timeout_seconds: int = Field(default=120, ge=10)
    llm_max_concurrency: int = Field(
        default=4,
        ge=1,
        le=32,
        description=(
            "Max concurrent in-flight LLM requests per pipeline run. "
            "Caps fan-out in the analysis/tailoring engines. Raising this "
            "can speed up large runs but increases the chance of hitting "
            "Anthropic rate limits. 1 = strictly sequential."
        ),
    )

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
    fetch_browser_timeout_ms: int = Field(
        default=20_000,
        ge=2_000,
        le=120_000,
        description="Navigation timeout for browser fetch page.goto() calls.",
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

    # --- Cover Letter Tailoring ---
    cover_letter_model: str = Field(
        default="claude-sonnet-4-6",
        description="Model for the cover-letter planning/generation pass.",
    )
    cover_letter_max_tokens: int = Field(
        default=2200,
        ge=512,
        description="Max output tokens for the cover-letter structured plan call.",
    )
    cover_letter_max_input_chars: int = Field(
        default=12_000,
        ge=2000,
        description="Safety cap on job-description chars sent to the cover-letter LLM call.",
    )
    cover_letter_style_guide_path: str = Field(
        default=str(
            _PROJECT_ROOT / "pipelines" / "job_agent" / "context" / "cover_letter_style.md"
        ),
        description="Path to local cover-letter style-guide markdown.",
    )
    cover_letter_output_dir: str = Field(
        default=str(_PROJECT_ROOT / "data" / "tailored_cover_letters"),
        description="Directory for generated tailored cover-letter .docx files.",
    )
    cover_letter_template_path: str = Field(
        default="",
        description="Optional template path for future cover-letter rendering customization.",
    )
    cover_letter_enable_critique: bool = Field(
        default=False,
        description="Enable optional critique pass for cover-letter generation.",
    )

    # --- Tailoring Cost Control ---
    tailoring_max_listings: int = Field(
        default=0,
        ge=0,
        description=(
            "Max listings to send through resume + cover-letter tailoring per run. "
            "0 = no cap (tailor all qualified). "
            "When set, the ranking node selects the top-N by salary (salary_max desc) "
            "and marks the rest SKIPPED. Discovery and job analysis still run on all listings."
        ),
    )

    # --- Discovery Node ---
    # Session persistence
    discovery_sessions_dir: str = Field(
        default=str(_PROJECT_ROOT / "data" / "sessions"),
        description="Directory for browser session storage_state JSON files (gitignored).",
    )

    # Concurrency
    discovery_max_concurrent_providers: int = Field(
        default=2,
        ge=1,
        le=6,
        description="Max browser providers running simultaneously (each uses one Playwright context).",
    )
    discovery_max_pages_per_search: int = Field(
        default=8,
        ge=1,
        le=30,
        description="Max search result pages to paginate per keyword/provider combination.",
    )
    discovery_max_listings_per_provider: int = Field(
        default=150,
        ge=10,
        description="Hard cap on listings collected per provider per run.",
    )
    discovery_session_max_age_hours: int = Field(
        default=72,
        ge=1,
        description="Treat saved session as stale if older than this many hours.",
    )

    # LinkedIn credentials
    linkedin_email: str = Field(
        default="", description="LinkedIn account email for job search login."
    )
    linkedin_password: SecretStr = Field(
        default=SecretStr(""),
        description="LinkedIn account password. Never logged.",
    )
    wellfound_email: str = Field(
        default="",
        description="Wellfound account email for job search login.",
    )
    wellfound_password: SecretStr = Field(
        default=SecretStr(""),
        description="Wellfound account password. Never logged.",
    )

    # Target company lists for ATS providers (comma-separated slugs)
    greenhouse_target_companies: str = Field(
        default="",
        description="Comma-separated Greenhouse company board slugs (e.g. 'anduril,palantir,scale-ai').",
    )
    lever_target_companies: str = Field(
        default="",
        description="Comma-separated Lever company slugs (e.g. 'openai,anthropic').",
    )
    workday_target_companies: str = Field(
        default="",
        description="Comma-separated 'company:subdomain' pairs for Workday (e.g. 'Anduril:anduril').",
    )
    direct_site_configs: str = Field(
        default="",
        description="Path to YAML file defining direct career-site scrape targets. Optional.",
    )

    # CAPTCHA handling
    captcha_strategy: Literal["avoid", "pause_notify", "solve"] = Field(
        default="pause_notify",
        description="CAPTCHA response strategy. 'solve' requires captcha_api_key.",
    )
    captcha_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="2captcha/anticaptcha API key. Only used when captcha_strategy='solve'.",
    )

    # Pre-filter
    discovery_prefilter_min_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Minimum rule-based fit score to include listing. 0.0 = accept everything.",
    )
    discovery_debug_capture_enabled: bool = Field(
        default=False,
        description="Capture screenshots/HTML/metadata for discovery failures.",
    )
    discovery_debug_capture_dir: str = Field(
        default=str(_PROJECT_ROOT / "data" / "debug_captures"),
        description="Directory for discovery failure-capture artifacts.",
    )
    discovery_debug_capture_html: bool = Field(
        default=True,
        description="Include HTML snapshots in discovery failure captures.",
    )

    # Provider enable flags
    # --- Filtering ---
    filter_allow_unknown_salary: bool = Field(
        default=True,
        description=(
            "When True (default), listings with no posted salary bypass the "
            "floor and reach tailoring. When False, missing-salary listings "
            "are dropped — useful if you want to enforce a hard floor and "
            "trust that desirable roles publish compensation bands."
        ),
    )

    discovery_linkedin_enabled: bool = Field(default=True)
    discovery_indeed_enabled: bool = Field(default=True)
    discovery_builtin_enabled: bool = Field(default=True)
    discovery_wellfound_enabled: bool = Field(default=False, description="Requires login.")
    discovery_greenhouse_enabled: bool = Field(default=True)
    discovery_lever_enabled: bool = Field(default=True)
    discovery_workday_enabled: bool = Field(
        default=False, description="Requires target company list."
    )

    # --- Scraper Pipeline ---
    scraper_dedup_db_path: str = Field(
        default=str(_PROJECT_ROOT / "data" / "scraper_dedup.db"),
        description="SQLite database for scraper deduplication.",
    )
    scraper_dedup_ttl_days: int = Field(
        default=90, ge=1, description="Days before pruning old dedup keys."
    )
    scraper_content_dir: str = Field(
        default=str(_PROJECT_ROOT / "data" / "scraper_content"),
        description="Base directory for JSONL content store.",
    )
    scraper_fixtures_dir: str = Field(
        default=str(_PROJECT_ROOT / "pipelines" / "scraper" / "fixtures"),
        description="Base directory for site fixture snapshots.",
    )
    scraper_profiles_dir: str = Field(
        default=str(_PROJECT_ROOT / "pipelines" / "scraper" / "profiles"),
        description="Base directory for site profile YAML files.",
    )

    # --- Self-Healing ---
    heal_reports_dir: str = Field(
        default=str(_PROJECT_ROOT / "data" / "heal_reports"),
        description="Directory for heal diagnosis reports.",
    )
    heal_max_tokens: int = Field(
        default=500_000, ge=10_000, description="Token budget for heal remediation."
    )
    heal_max_retries_per_step: int = Field(
        default=3, ge=1, le=10, description="Max retries per remediation step."
    )
    heal_max_wall_clock_minutes: int = Field(
        default=30, ge=5, description="Wall-clock cap for heal remediation."
    )
    heal_diagnosis_model: str = Field(
        default="claude-sonnet-4-6",
        description="Model for heal diagnosis pass.",
    )
    heal_remediation_model: str = Field(
        default="claude-sonnet-4-6",
        description="Model for heal remediation agent.",
    )

    # --- IMAP (heal reply watching) ---
    imap_host: str = Field(default="", description="IMAP server for heal reply watching.")
    imap_port: int = Field(default=993, description="IMAP port (993 for SSL).")
    imap_username: str = Field(default="", description="IMAP username.")
    imap_password: SecretStr = Field(default=SecretStr(""), description="IMAP password.")
    heal_reply_poll_interval_s: int = Field(
        default=300, ge=30, description="Seconds between inbox polls for heal replies."
    )
    heal_trigger_signing_secret: SecretStr = Field(
        default=SecretStr(""),
        description="HMAC secret used to sign and verify heal reply trigger tokens.",
    )
    heal_trigger_token_ttl_s: int = Field(
        default=86_400,
        ge=60,
        description="Maximum age (seconds) for a heal reply trigger token.",
    )
    heal_reply_allowed_senders: str = Field(
        default="",
        description="Comma-separated allowed sender email addresses for heal replies.",
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

    @property
    def greenhouse_company_list(self) -> list[str]:
        """Parse greenhouse_target_companies into a list of slugs."""
        return [s.strip() for s in self.greenhouse_target_companies.split(",") if s.strip()]

    @property
    def lever_company_list(self) -> list[str]:
        """Parse lever_target_companies into a list of slugs."""
        return [s.strip() for s in self.lever_target_companies.split(",") if s.strip()]

    @property
    def workday_company_list(self) -> list[str]:
        """Parse workday_target_companies into a list of 'Name:subdomain[:wdN]' entries."""
        return [s.strip() for s in self.workday_target_companies.split(",") if s.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance.

    Cached so that repeated calls don't re-parse the environment.
    Call ``get_settings.cache_clear()`` in tests to reset.
    """
    return Settings()
