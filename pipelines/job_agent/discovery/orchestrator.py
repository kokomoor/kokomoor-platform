"""DiscoveryOrchestrator -- coordinates all provider adapters.

Runs browser providers concurrently under a semaphore (to limit simultaneous
Playwright contexts). HTTP providers run without a semaphore (they're cheap).

For each browser provider:
1. Load existing session from SessionStore.
2. Create BrowserManager with that session's storage_state.
3. Navigate to the provider's home domain (warm-up nav, establishes context).
4. Check authentication. Re-authenticate if needed.
5. Run search via the provider's run_search() method.
6. Save session back to SessionStore.
7. Return ProviderResult.

Auth retry policy: if authentication fails with a loaded session, the session
is invalidated and the provider is retried once with a fresh browser context.
This handles stale sessions that cause unexpected auth pages.

Failures in any provider are isolated -- they produce an error-annotated
ProviderResult but do not propagate exceptions to the caller.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from core.browser import BrowserManager
from core.browser.captcha import CaptchaHandler
from core.browser.debug_capture import FailureCapture
from core.browser.human_behavior import HumanBehavior
from core.browser.session import SessionStore
from pipelines.job_agent.discovery.models import ProviderResult
from pipelines.job_agent.discovery.providers.builtin import BuiltInProvider
from pipelines.job_agent.discovery.providers.direct_site import DirectSiteProvider
from pipelines.job_agent.discovery.providers.greenhouse import (
    fetch_all_greenhouse_companies,
)
from pipelines.job_agent.discovery.providers.indeed import IndeedProvider
from pipelines.job_agent.discovery.providers.lever import fetch_all_lever_companies
from pipelines.job_agent.discovery.providers.linkedin import LinkedInProvider
from pipelines.job_agent.discovery.providers.wellfound import WellfoundProvider
from pipelines.job_agent.discovery.providers.workday import WorkdayProvider
from pipelines.job_agent.discovery.rate_limiter import DomainRateLimiter
from pipelines.job_agent.models import JobSource

if TYPE_CHECKING:
    from core.config import Settings
    from pipelines.job_agent.discovery.models import DiscoveryConfig, ListingRef
    from pipelines.job_agent.discovery.providers.base import BaseProvider
    from pipelines.job_agent.models import SearchCriteria

logger = structlog.get_logger(__name__)


class DiscoveryOrchestrator:
    """Fan out to enabled providers, aggregate and return raw refs."""

    def __init__(self) -> None:
        self.last_provider_results: list[ProviderResult] = []

    async def run(
        self,
        criteria: SearchCriteria,
        config: DiscoveryConfig,
        settings: Settings,
        *,
        run_id: str = "",
    ) -> list[ListingRef]:
        """Execute all enabled providers and return aggregated ListingRefs."""
        session_store = SessionStore(Path(config.sessions_dir))
        semaphore = asyncio.Semaphore(config.max_concurrent_providers)
        tasks: list[asyncio.Task[ProviderResult]] = []
        capture = FailureCapture(
            enabled=config.debug_capture_enabled,
            base_dir=config.debug_capture_dir,
            run_id=run_id or "discovery",
            include_html=config.debug_capture_html,
        )

        loop = asyncio.get_running_loop()

        requested_sources = set(criteria.sources) if criteria.sources else None

        if (
            config.greenhouse_enabled
            and config.greenhouse_companies
            and (requested_sources is None or JobSource.GREENHOUSE in requested_sources)
        ):
            tasks.append(
                loop.create_task(
                    self._run_http_greenhouse(
                        config.greenhouse_companies,
                        criteria,
                        config,
                        capture,
                    )
                )
            )
        if (
            config.lever_enabled
            and config.lever_companies
            and (requested_sources is None or JobSource.LEVER in requested_sources)
        ):
            tasks.append(
                loop.create_task(
                    self._run_http_lever(
                        config.lever_companies,
                        criteria,
                        config,
                        capture,
                    )
                )
            )

        browser_providers = self._get_enabled_browser_providers(
            config, requested_sources=requested_sources
        )
        for provider in browser_providers:
            tasks.append(
                loop.create_task(
                    self._run_browser_provider(
                        provider,
                        criteria,
                        config,
                        settings,
                        session_store,
                        semaphore,
                        capture,
                    )
                )
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)
        provider_results: list[ProviderResult] = []

        all_refs: list[ListingRef] = []
        for result in results:
            if isinstance(result, BaseException):
                logger.error(
                    "orchestrator.provider_exception",
                    error=str(result)[:300],
                )
            elif isinstance(result, ProviderResult):
                provider_results.append(result)
                all_refs.extend(result.refs)
                if result.errors:
                    logger.warning(
                        "orchestrator.provider_errors",
                        source=result.source.value,
                        errors=result.errors[:3],
                    )
                logger.info(
                    "orchestrator.provider_complete",
                    source=result.source.value,
                    refs=len(result.refs),
                    pages=result.pages_scraped,
                )
        self.last_provider_results = provider_results

        logger.info(
            "orchestrator.run_complete",
            total_refs=len(all_refs),
            providers_run=len(tasks),
        )
        return all_refs

    # ------------------------------------------------------------------
    # Provider dispatch
    # ------------------------------------------------------------------

    @staticmethod
    def _get_enabled_browser_providers(
        config: DiscoveryConfig,
        *,
        requested_sources: set[JobSource] | None,
    ) -> list[BaseProvider]:
        """Return browser providers that are both config-enabled and user-requested."""
        providers: list[BaseProvider] = []

        def _wanted(source: JobSource) -> bool:
            return requested_sources is None or source in requested_sources

        if config.linkedin_enabled and _wanted(JobSource.LINKEDIN):
            providers.append(LinkedInProvider())
        if config.indeed_enabled and _wanted(JobSource.INDEED):
            providers.append(IndeedProvider())
        if config.builtin_enabled and _wanted(JobSource.BUILTIN):
            providers.append(BuiltInProvider())
        if config.wellfound_enabled and _wanted(JobSource.WELLFOUND):
            providers.append(WellfoundProvider())
        if config.workday_enabled and config.workday_companies and _wanted(JobSource.WORKDAY):
            providers.append(WorkdayProvider())
        if config.direct_site_configs and _wanted(JobSource.COMPANY_SITE):
            providers.append(DirectSiteProvider())
        return providers

    # ------------------------------------------------------------------
    # Browser provider runner
    # ------------------------------------------------------------------

    @staticmethod
    def _credentials_for_provider(
        provider: BaseProvider,
        settings: Settings,
    ) -> tuple[str, str] | None:
        """Return provider-specific credentials or None if unavailable."""
        if provider.source == JobSource.LINKEDIN:
            email = settings.linkedin_email.strip()
            password = settings.linkedin_password.get_secret_value()
            return (email, password) if email and password else None
        if provider.source == JobSource.WELLFOUND:
            email = settings.wellfound_email.strip()
            password = settings.wellfound_password.get_secret_value()
            return (email, password) if email and password else None
        return None

    @staticmethod
    async def _run_browser_provider(
        provider: BaseProvider,
        criteria: SearchCriteria,
        config: DiscoveryConfig,
        settings: Settings,
        session_store: SessionStore,
        semaphore: asyncio.Semaphore,
        capture: FailureCapture,
    ) -> ProviderResult:
        async with semaphore:
            source = provider.source

            # Proactively discard sessions older than the configured max age.
            session_age = session_store.age_hours(source)
            if session_age is not None and session_age > config.session_max_age_hours:
                logger.info(
                    "orchestrator.session_expired",
                    source=source.value,
                    age_hours=round(session_age, 1),
                    max_hours=config.session_max_age_hours,
                )
                session_store.invalidate(source)

            storage_state = session_store.load(source)
            had_session = storage_state is not None

            if had_session:
                logger.info(
                    "orchestrator.session_loaded",
                    source=source.value,
                    age_hours=session_store.age_hours(source),
                )
            else:
                logger.info("orchestrator.no_session", source=source.value)

            result = await DiscoveryOrchestrator._attempt_browser_provider(
                provider=provider,
                criteria=criteria,
                config=config,
                settings=settings,
                session_store=session_store,
                capture=capture,
                storage_state=storage_state,
            )

            # Retry policy: if auth failed and we had a session, retry
            # with a fresh browser context. The failed attempt already
            # invalidated the on-disk session via the finally block in
            # ``_attempt_browser_provider``, so no second invalidate
            # call is needed here.
            auth_failed = any(
                err.startswith("auth_failed") or err.startswith("auth_missing")
                for err in result.errors
            )
            if auth_failed and had_session and provider.requires_auth():
                logger.info(
                    "orchestrator.auth_retry_with_fresh_session",
                    source=source.value,
                )
                result = await DiscoveryOrchestrator._attempt_browser_provider(
                    provider=provider,
                    criteria=criteria,
                    config=config,
                    settings=settings,
                    session_store=session_store,
                    capture=capture,
                    storage_state=None,
                )

            return result

    @staticmethod
    async def _attempt_browser_provider(
        *,
        provider: BaseProvider,
        criteria: SearchCriteria,
        config: DiscoveryConfig,
        settings: Settings,
        session_store: SessionStore,
        capture: FailureCapture,
        storage_state: dict[str, object] | None,
    ) -> ProviderResult:
        """Single attempt to run a browser provider.

        Session save policy: we only persist the browser's storage_state
        when authentication succeeded (or wasn't required). Saving a
        session mid-auth-failure captures cookies from a half-completed
        login flow — LinkedIn interstitials, CAPTCHA challenges, expired
        CSRF tokens — which poisons the next run.  The 2026-04-14 run
        hit this: run 2 failed auth, saved the bad session, run 3 loaded
        it and also failed auth (because the session was corrupted).
        """
        source = provider.source
        behavior = HumanBehavior()
        rate_limiter = DomainRateLimiter(source)
        captcha_handler = CaptchaHandler()

        try:
            async with BrowserManager(storage_state=storage_state) as browser:
                page = await browser.new_page()
                refs: list[ListingRef] = []
                errors: list[str] = []
                saved = False
                auth_ok = False  # Flip to True only after a successful auth

                try:
                    if provider.requires_auth():
                        await rate_limiter.wait()
                        try:
                            await page.goto(
                                f"https://{provider.base_domain()}",
                                wait_until="domcontentloaded",
                                timeout=20_000,
                            )
                            await behavior.reading_pause(800)
                        except Exception:
                            logger.warning(
                                "orchestrator.warmup_nav_failed",
                                source=source.value,
                                exc_info=True,
                            )
                            artifacts = await capture.capture_page_failure(
                                source=source,
                                stage="warmup_nav_failed",
                                reason="provider_warmup_navigation_failed",
                                page=page,
                            )
                            errors.append(f"warmup_nav_failed:{artifacts[0] if artifacts else ''}")

                        if not await provider.is_authenticated(page):
                            logger.info(
                                "orchestrator.authenticating",
                                source=source.value,
                            )
                            credentials = DiscoveryOrchestrator._credentials_for_provider(
                                provider,
                                settings,
                            )
                            if credentials is None:
                                logger.warning(
                                    "orchestrator.auth_missing_credentials",
                                    source=source.value,
                                )
                                artifacts = await capture.capture_page_failure(
                                    source=source,
                                    stage="auth_missing_credentials",
                                    reason="provider_requires_credentials_but_none_configured",
                                    page=page,
                                )
                                return ProviderResult(
                                    source=source,
                                    refs=[],
                                    errors=[
                                        "auth_missing_credentials",
                                        *([f"capture:{artifacts[0]}"] if artifacts else []),
                                    ],
                                    pages_scraped=0,
                                    session_saved=False,
                                )
                            email, password = credentials
                            success = await provider.authenticate(
                                page,
                                email=email,
                                password=password,
                                behavior=behavior,
                            )
                            if not success:
                                logger.warning(
                                    "orchestrator.auth_failed",
                                    source=source.value,
                                )
                                artifacts = await capture.capture_page_failure(
                                    source=source,
                                    stage="auth_failed",
                                    reason="provider_authenticate_returned_false",
                                    page=page,
                                )
                                return ProviderResult(
                                    source=source,
                                    refs=[],
                                    errors=[
                                        "auth_failed",
                                        *([f"capture:{artifacts[0]}"] if artifacts else []),
                                    ],
                                    pages_scraped=0,
                                    session_saved=False,
                                )
                            auth_ok = True
                        else:
                            logger.info(
                                "orchestrator.already_authenticated",
                                source=source.value,
                            )
                            auth_ok = True
                    else:
                        # Providers that don't require auth (unused today
                        # for browser providers but kept for future
                        # public-site scrapers) skip the whole auth block;
                        # their session, such as it exists, is always
                        # worth persisting.
                        auth_ok = True

                    refs = await provider.run_search(
                        page,
                        criteria,
                        config,
                        behavior=behavior,
                        rate_limiter=rate_limiter,
                        captcha_handler=captcha_handler,
                        capture=capture,
                    )
                except Exception as exc:
                    logger.exception("orchestrator.provider_failed", source=source.value)
                    artifacts = await capture.capture_page_failure(
                        source=source,
                        stage="provider_exception",
                        reason="provider_search_raised_exception",
                        page=page,
                        error=str(exc),
                    )
                    errors = [
                        f"provider_exception:{exc.__class__.__name__}:{str(exc)[:220]}",
                        *([f"capture:{artifacts[0]}"] if artifacts else []),
                    ]
                finally:
                    if auth_ok:
                        saved = await session_store.save(source, browser)
                    else:
                        # Ensure any previously-loaded poisoned session is
                        # removed so the next run starts clean. This is the
                        # belt-and-braces counterpart to the retry policy
                        # in _run_browser_provider, which handles the
                        # same-run retry case.
                        session_store.invalidate(source)
                        logger.info(
                            "orchestrator.session_skipped_save_auth_failed",
                            source=source.value,
                        )

                return ProviderResult(
                    source=source,
                    refs=refs,
                    errors=errors,
                    pages_scraped=rate_limiter.page_count,
                    session_saved=saved,
                )

        except Exception as exc:
            logger.exception("orchestrator.browser_launch_failed", source=source.value)
            artifacts = capture.capture_metadata_failure(
                source=source,
                stage="browser_launch_failed",
                reason="browser_manager_launch_or_context_failed",
                error=str(exc),
            )
            return ProviderResult(
                source=source,
                refs=[],
                errors=[
                    f"browser_launch_failed:{exc.__class__.__name__}:{str(exc)[:220]}",
                    *([f"capture:{artifacts[0]}"] if artifacts else []),
                ],
                pages_scraped=0,
                session_saved=False,
            )

    # ------------------------------------------------------------------
    # HTTP provider runners
    # ------------------------------------------------------------------

    @staticmethod
    async def _run_http_greenhouse(
        companies: list[str],
        criteria: SearchCriteria,
        config: DiscoveryConfig,
        capture: FailureCapture,
    ) -> ProviderResult:
        try:
            refs = await fetch_all_greenhouse_companies(companies, criteria, config)
            return ProviderResult(
                source=JobSource.GREENHOUSE,
                refs=refs,
                errors=[],
                pages_scraped=len(companies),
                session_saved=False,
            )
        except Exception as exc:
            logger.exception("orchestrator.greenhouse_failed")
            artifacts = capture.capture_metadata_failure(
                source=JobSource.GREENHOUSE,
                stage="http_provider_exception",
                reason="greenhouse_provider_runner_failed",
                error=str(exc),
                extra={"companies": companies},
            )
            return ProviderResult(
                source=JobSource.GREENHOUSE,
                refs=[],
                errors=[
                    f"http_provider_exception:{exc.__class__.__name__}:{str(exc)[:220]}",
                    *([f"capture:{artifacts[0]}"] if artifacts else []),
                ],
                pages_scraped=0,
                session_saved=False,
            )

    @staticmethod
    async def _run_http_lever(
        companies: list[str],
        criteria: SearchCriteria,
        config: DiscoveryConfig,
        capture: FailureCapture,
    ) -> ProviderResult:
        try:
            refs = await fetch_all_lever_companies(companies, criteria, config)
            return ProviderResult(
                source=JobSource.LEVER,
                refs=refs,
                errors=[],
                pages_scraped=len(companies),
                session_saved=False,
            )
        except Exception as exc:
            logger.exception("orchestrator.lever_failed")
            artifacts = capture.capture_metadata_failure(
                source=JobSource.LEVER,
                stage="http_provider_exception",
                reason="lever_provider_runner_failed",
                error=str(exc),
                extra={"companies": companies},
            )
            return ProviderResult(
                source=JobSource.LEVER,
                refs=[],
                errors=[
                    f"http_provider_exception:{exc.__class__.__name__}:{str(exc)[:220]}",
                    *([f"capture:{artifacts[0]}"] if artifacts else []),
                ],
                pages_scraped=0,
                session_saved=False,
            )
