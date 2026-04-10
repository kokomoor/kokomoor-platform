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

Failures in any provider are isolated -- they produce an error-annotated
ProviderResult but do not propagate exceptions to the caller.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from core.browser import BrowserManager
from pipelines.job_agent.discovery.captcha import CaptchaHandler
from pipelines.job_agent.discovery.human_behavior import HumanBehavior
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
from pipelines.job_agent.discovery.session import SessionStore
from pipelines.job_agent.models import JobSource

if TYPE_CHECKING:
    from core.config import Settings
    from pipelines.job_agent.discovery.models import DiscoveryConfig, ListingRef
    from pipelines.job_agent.discovery.providers.base import BaseProvider
    from pipelines.job_agent.models import SearchCriteria

logger = structlog.get_logger(__name__)


class DiscoveryOrchestrator:
    """Fan out to enabled providers, aggregate and return raw refs."""

    async def run(
        self,
        criteria: SearchCriteria,
        config: DiscoveryConfig,
        settings: Settings,
    ) -> list[ListingRef]:
        """Execute all enabled providers and return aggregated ListingRefs."""
        session_store = SessionStore(Path(config.sessions_dir))
        semaphore = asyncio.Semaphore(config.max_concurrent_providers)
        tasks: list[asyncio.Task[ProviderResult]] = []

        loop = asyncio.get_running_loop()

        requested_sources = set(criteria.sources) if criteria.sources else None

        if (
            config.greenhouse_enabled
            and config.greenhouse_companies
            and (requested_sources is None or JobSource.GREENHOUSE in requested_sources)
        ):
            tasks.append(
                loop.create_task(
                    self._run_http_greenhouse(config.greenhouse_companies, criteria, config)
                )
            )
        if (
            config.lever_enabled
            and config.lever_companies
            and (requested_sources is None or JobSource.LEVER in requested_sources)
        ):
            tasks.append(
                loop.create_task(self._run_http_lever(config.lever_companies, criteria, config))
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
                    )
                )
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_refs: list[ListingRef] = []
        for result in results:
            if isinstance(result, BaseException):
                logger.error(
                    "orchestrator.provider_exception",
                    error=str(result)[:300],
                )
            elif isinstance(result, ProviderResult):
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
    async def _run_browser_provider(
        provider: BaseProvider,
        criteria: SearchCriteria,
        config: DiscoveryConfig,
        settings: Settings,
        session_store: SessionStore,
        semaphore: asyncio.Semaphore,
    ) -> ProviderResult:
        async with semaphore:
            source = provider.source
            storage_state = session_store.load(source)
            if storage_state is not None:
                logger.info(
                    "orchestrator.session_loaded",
                    source=source.value,
                    age_hours=session_store.age_hours(source),
                )
            else:
                logger.info("orchestrator.no_session", source=source.value)

            behavior = HumanBehavior()
            rate_limiter = DomainRateLimiter(source)
            captcha_handler = CaptchaHandler()

            try:
                async with BrowserManager(storage_state=storage_state) as browser:
                    page = await browser.new_page()
                    refs: list[ListingRef] = []
                    errors: list[str] = []
                    saved = False

                    try:
                        if provider.requires_auth():
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

                            if not await provider.is_authenticated(page):
                                logger.info(
                                    "orchestrator.authenticating",
                                    source=source.value,
                                )
                                success = await provider.authenticate(
                                    page,
                                    email=settings.linkedin_email,
                                    password=settings.linkedin_password.get_secret_value(),
                                    behavior=behavior,
                                )
                                if not success:
                                    logger.warning(
                                        "orchestrator.auth_failed",
                                        source=source.value,
                                    )
                                    return ProviderResult(
                                        source=source,
                                        refs=[],
                                        errors=["auth_failed"],
                                        pages_scraped=0,
                                        session_saved=False,
                                    )

                        refs = await provider.run_search(
                            page,
                            criteria,
                            config,
                            behavior=behavior,
                            rate_limiter=rate_limiter,
                            captcha_handler=captcha_handler,
                        )
                    except Exception as exc:
                        logger.exception("orchestrator.provider_failed", source=source.value)
                        errors = [str(exc)[:300]]
                    finally:
                        saved = await session_store.save(source, browser)

                    return ProviderResult(
                        source=source,
                        refs=refs,
                        errors=errors,
                        pages_scraped=rate_limiter.page_count,
                        session_saved=saved,
                    )

            except Exception as exc:
                logger.exception("orchestrator.browser_launch_failed", source=source.value)
                return ProviderResult(
                    source=source,
                    refs=[],
                    errors=[str(exc)[:300]],
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
            return ProviderResult(
                source=JobSource.GREENHOUSE,
                refs=[],
                errors=[str(exc)[:300]],
                pages_scraped=0,
                session_saved=False,
            )

    @staticmethod
    async def _run_http_lever(
        companies: list[str],
        criteria: SearchCriteria,
        config: DiscoveryConfig,
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
            return ProviderResult(
                source=JobSource.LEVER,
                refs=[],
                errors=[str(exc)[:300]],
                pages_scraped=0,
                session_saved=False,
            )
