"""LangGraph application node orchestrator.

Refactored to support a registry-based submitter dispatch, persistent
stealth session management, and adaptive jittered delays.
"""

from __future__ import annotations

import asyncio
import inspect
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from core.browser import BrowserManager
from core.browser.human_behavior import HumanBehavior
from core.browser.session import SessionStore
from core.config import Settings, get_settings
from core.fetch.http_client import HttpFetcher
from core.observability.metrics import (
    APPLICATION_ATTEMPTS,
    APPLICATION_FIELDS_FILLED,
    APPLICATION_LLM_QA_CALLS,
)
from pipelines.job_agent.application import SubmissionStrategy, route_application
from pipelines.job_agent.application._debug import capture_application_failure
from pipelines.job_agent.application.dedup import ApplicationDedupStore
from pipelines.job_agent.application.notifications import notify_application_batch_summary
from pipelines.job_agent.application.qa_answerer import QACache
from pipelines.job_agent.application.registry import get_submitter
from pipelines.job_agent.models import (
    ApplicationAttempt,
    ApplicationStatus,
    load_application_profile,
)
from pipelines.job_agent.state import JobAgentState, PipelinePhase

if TYPE_CHECKING:
    import httpx
    from playwright.async_api import Page

    from core.llm.protocol import LLMClient
    from pipelines.job_agent.models import CandidateApplicationProfile, JobListing

logger = structlog.get_logger(__name__)


async def application_node(
    state: JobAgentState,
    *,
    llm_client: LLMClient | None = None,
) -> JobAgentState:
    """Attempt applications for listings that reached the tailoring phase."""
    state.phase = PipelinePhase.APPLICATION
    settings = get_settings()
    log = logger.bind(run_id=state.run_id)

    if not state.tailored_listings:
        log.info("application.no_tailored_listings")
        state.application_results = []
        return state

    profile = load_application_profile(Path(settings.candidate_application_profile_path))

    dedup_store = ApplicationDedupStore()

    to_apply = await dedup_store.filter_unapplied(state.tailored_listings)
    processable = [li for li in to_apply if li.status != ApplicationStatus.ERRORED]

    if len(to_apply) < len(state.tailored_listings):
        log.info(
            "application.deduplicated",
            skipped=len(state.tailored_listings) - len(to_apply),
        )

    max_per_run = settings.application_max_per_run
    if max_per_run > 0:
        processable = processable[:max_per_run]

    attempts: list[ApplicationAttempt] = []
    attempt_pairs: list[tuple[JobListing, ApplicationAttempt]] = []
    total = len(processable)

    cache = QACache()

    browser_listings: list[tuple[JobListing, SubmissionStrategy]] = []
    api_listings: list[tuple[JobListing, SubmissionStrategy]] = []

    for listing in processable:
        route = await route_application(listing)
        if route.requires_browser:
            browser_listings.append((listing, route.strategy))
        else:
            api_listings.append((listing, route.strategy))

    # 1. API Submissions
    fetcher = HttpFetcher()
    async with fetcher.create_client() as client:
        for index, (listing, strategy) in enumerate(api_listings):
            attempt = await _apply_with_retry(
                listing=listing,
                strategy=strategy,
                profile=profile,
                client=client,
                llm_client=llm_client,
                settings=settings,
                run_id=state.run_id,
                dry_run=state.dry_run,
                cache=cache,
            )
            attempts.append(attempt)
            attempt_pairs.append((listing, attempt))
            await _handle_attempt_outcome(state, listing, attempt, dedup_store)
            await _jittered_delay(index, len(api_listings), state.dry_run, settings)

    # 2. Browser Submissions — single BrowserManager lifecycle for the batch
    if browser_listings:
        session_store = SessionStore(Path("data/sessions"))
        has_linkedin = any(
            strategy == SubmissionStrategy.TEMPLATE_LINKEDIN_EASY_APPLY
            for _, strategy in browser_listings
        )
        async with BrowserManager(storage_state=session_store.load("linkedin")) as manager:
            # Warm up the session before touching any job URLs. The
            # 2026-04-14 run loaded a freshly-saved discovery session,
            # navigated straight to `/jobs/view/<id>`, and hit a
            # logged-out render (gray "Me" silhouette, no Easy Apply
            # modal wiring). Discovery had saved the session while
            # captcha challenges were active, which leaves the cookies
            # in a "flagged" state — valid enough for LinkedIn to serve
            # the search results, stale enough to degrade into the
            # public job-view when opened in a fresh context. Re-auth
            # here shifts that failure from three opaque
            # button-not-found errors into a single clean retry.
            linkedin_auth_ok = False
            if has_linkedin:
                linkedin_auth_ok = await _ensure_linkedin_authenticated(
                    manager, settings, log
                )
                if not linkedin_auth_ok:
                    log.warning(
                        "application.linkedin_auth_warmup_failed",
                        listings=sum(
                            1
                            for _, s in browser_listings
                            if s == SubmissionStrategy.TEMPLATE_LINKEDIN_EASY_APPLY
                        ),
                    )

            for index, (listing, strategy) in enumerate(browser_listings):
                if (
                    strategy == SubmissionStrategy.TEMPLATE_LINKEDIN_EASY_APPLY
                    and not linkedin_auth_ok
                ):
                    attempt = ApplicationAttempt(
                        dedup_key=listing.dedup_key,
                        status="stuck",
                        strategy=strategy.value,
                        summary=(
                            "LinkedIn authentication warmup failed; skipping "
                            "Easy Apply to avoid cascading bot detection. "
                            "Resolve the login (solve captcha in headed mode "
                            "or refresh credentials) and retry."
                        ),
                    )
                else:
                    page = await manager.new_page()
                    try:
                        attempt = await _apply_with_retry(
                            listing=listing,
                            strategy=strategy,
                            profile=profile,
                            client=None,
                            page=page,
                            llm_client=llm_client,
                            settings=settings,
                            run_id=state.run_id,
                            dry_run=state.dry_run,
                            cache=cache,
                        )
                    finally:
                        await page.close()

                attempts.append(attempt)
                attempt_pairs.append((listing, attempt))
                await _handle_attempt_outcome(state, listing, attempt, dedup_store)
                await _jittered_delay(index, len(browser_listings), state.dry_run, settings)

            # Persist the authenticated state so the next run inherits
            # a clean session. If warmup failed, invalidate instead so
            # we don't re-poison the next run.
            if has_linkedin:
                if linkedin_auth_ok:
                    try:
                        await session_store.save("linkedin", manager)
                    except Exception:
                        log.warning(
                            "application.session_save_failed",
                            exc_info=True,
                        )
                else:
                    session_store.invalidate("linkedin")

    dedup_store.close()

    await notify_application_batch_summary(attempt_pairs)

    state.application_results = attempts
    log.info(
        "application.complete",
        attempted=total,
        submitted=sum(1 for a in attempts if a.status == "submitted"),
        awaiting_review=sum(1 for a in attempts if a.status == "awaiting_review"),
        stuck=sum(1 for a in attempts if a.status == "stuck"),
        errors=sum(1 for a in attempts if a.status == "error"),
    )
    return state


async def _ensure_linkedin_authenticated(
    manager: BrowserManager,
    settings: Settings,
    log: Any,
) -> bool:
    """Verify the browser context can render LinkedIn in a logged-in state.

    Warms up the context against ``www.linkedin.com``, checks
    ``LinkedInProvider.is_authenticated`` (positive DOM + URL signals),
    and falls back to the full login flow when the cookies loaded from
    the session file don't resolve into an authenticated view. Reuses
    the same provider the discovery phase uses so we get free coverage
    of every auth mode (welcome-back card, password-only, full login).

    A return value of ``False`` is authoritative: the caller should
    short-circuit every LinkedIn listing in the batch and avoid
    re-saving the poisoned context back to disk.
    """
    from pipelines.job_agent.discovery.providers.linkedin import LinkedInProvider

    email = settings.linkedin_email.strip()
    password = settings.linkedin_password.get_secret_value()
    if not email or not password:
        log.warning("application.linkedin_credentials_missing")
        return False

    provider = LinkedInProvider()
    behavior = HumanBehavior()
    page = await manager.new_page()
    try:
        try:
            await page.goto(
                f"https://{provider.base_domain()}",
                wait_until="domcontentloaded",
                timeout=20_000,
            )
        except Exception:
            log.warning("application.linkedin_warmup_nav_failed", exc_info=True)
            return False

        await behavior.reading_pause(800)

        if await provider.is_authenticated(page):
            log.info("application.linkedin_session_valid")
            return True

        log.info("application.linkedin_session_stale_reauthenticating")
        try:
            success = await provider.authenticate(
                page,
                email=email,
                password=password,
                behavior=behavior,
            )
        except Exception:
            log.exception("application.linkedin_reauth_crashed")
            return False

        if success and await provider.is_authenticated(page):
            log.info("application.linkedin_reauth_success")
            return True

        log.warning("application.linkedin_reauth_failed")
        return False
    finally:
        await page.close()


async def _jittered_delay(index: int, total: int, dry_run: bool, settings: Settings) -> None:
    if index < total - 1 and not dry_run:
        base_delay = settings.application_min_delay_seconds
        jitter = random.uniform(0.8, 1.4)
        await asyncio.sleep(base_delay * jitter)


async def _apply_with_retry(
    *,
    listing: JobListing,
    strategy: SubmissionStrategy,
    profile: CandidateApplicationProfile,
    client: httpx.AsyncClient | None,
    page: Page | None = None,
    llm_client: LLMClient | None,
    settings: Settings,
    run_id: str,
    dry_run: bool,
    cache: QACache | None = None,
    max_retries: int = 1,
) -> ApplicationAttempt:
    """Attempt application with one retry on hard errors.

    ``stuck`` is a logic wall (CAPTCHA, account creation, daily cap) and is
    never retried — the second attempt would just hit the same wall.
    """
    attempt = await _apply_to_listing(
        listing=listing,
        strategy=strategy,
        profile=profile,
        client=client,
        page=page,
        llm_client=llm_client,
        settings=settings,
        run_id=run_id,
        dry_run=dry_run,
        cache=cache,
    )

    for retry_num in range(1, max_retries + 1):
        if attempt.status != "error":
            return attempt
        logger.bind(dedup_key=listing.dedup_key, strategy=strategy.value).info(
            "application.retry_attempt", attempt=retry_num
        )
        attempt = await _apply_to_listing(
            listing=listing,
            strategy=strategy,
            profile=profile,
            client=client,
            page=page,
            llm_client=llm_client,
            settings=settings,
            run_id=run_id,
            dry_run=dry_run,
            cache=cache,
        )

    return attempt


_STRATEGY_PREFIXES = ("api_", "template_", "agent_")


def _platform_from_strategy(strategy: str) -> str:
    """Strip the one prefix (api_/template_/agent_) from a strategy value."""
    for prefix in _STRATEGY_PREFIXES:
        if strategy.startswith(prefix):
            return strategy[len(prefix) :]
    return strategy


async def _handle_attempt_outcome(
    state: JobAgentState,
    listing: JobListing,
    attempt: ApplicationAttempt,
    dedup_store: ApplicationDedupStore,
) -> None:
    """Update listing/application state, dedup store, and metrics."""
    _apply_status_transition(state, listing, attempt)

    platform = _platform_from_strategy(attempt.strategy)

    # Surface failure reasons in the pipeline log. Without this, a submitter
    # that returns ``status="error"`` via ApplicationAttempt (rather than
    # raising) leaves only a `failure_capture.saved` breadcrumb — the summary
    # text sits unused inside state.errors until end-of-run aggregation. The
    # 2026-04-14 run produced three of these opaque failures before we
    # realised the Easy Apply selectors had gone stale.
    if attempt.status == "error":
        logger.warning(
            "application.attempt_errored",
            dedup_key=listing.dedup_key,
            run_id=state.run_id,
            strategy=attempt.strategy,
            platform=platform,
            summary=attempt.summary[:300],
            screenshot_path=attempt.screenshot_path or None,
        )
    elif attempt.status == "stuck":
        logger.info(
            "application.attempt_stuck",
            dedup_key=listing.dedup_key,
            run_id=state.run_id,
            strategy=attempt.strategy,
            platform=platform,
            summary=attempt.summary[:300],
        )

    APPLICATION_ATTEMPTS.labels(
        platform=platform,
        strategy=attempt.strategy,
        status=attempt.status,
    ).inc()

    if attempt.fields_filled > 0:
        APPLICATION_FIELDS_FILLED.labels(platform=platform).inc(attempt.fields_filled)

    if attempt.llm_calls_made > 0:
        APPLICATION_LLM_QA_CALLS.labels(platform=platform).inc(attempt.llm_calls_made)

    if attempt.status in ("submitted", "awaiting_review"):
        await dedup_store.mark_applied(
            listing,
            strategy=attempt.strategy,
            status=attempt.status,
            artifact_dir=attempt.screenshot_path or "",
        )


async def _apply_to_listing(
    *,
    listing: JobListing,
    strategy: SubmissionStrategy,
    profile: CandidateApplicationProfile,
    client: httpx.AsyncClient | None,
    page: Page | None = None,
    llm_client: LLMClient | None,
    settings: Settings,
    run_id: str,
    dry_run: bool,
    cache: QACache | None = None,
) -> ApplicationAttempt:
    """Apply to one listing using the unified submitter registry."""
    submitter = get_submitter(strategy)
    if not submitter:
        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="error",
            strategy=strategy.value,
            summary=f"No registered handler for strategy: {strategy.value}",
        )

    if not listing.tailored_resume_path:
        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="stuck",
            strategy=strategy.value,
            summary="Tailored resume path missing; tailoring never produced an artifact.",
        )

    resume_path = Path(listing.tailored_resume_path)
    if not resume_path.exists():
        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="stuck",
            strategy=strategy.value,
            summary=f"Tailored resume artifact missing on disk: {resume_path}",
        )

    cover_letter_path = (
        Path(listing.tailored_cover_letter_path) if listing.tailored_cover_letter_path else None
    )

    dry_run_mode = dry_run or settings.application_require_human_review

    candidate_kwargs: dict[str, Any] = {
        "listing": listing,
        "profile": profile,
        "resume_path": resume_path,
        "cover_letter_path": cover_letter_path,
        "client": client,
        "page": page,
        "llm": llm_client,
        "run_id": run_id,
        "dry_run": dry_run_mode,
        "behavior": HumanBehavior(),
        "cache": cache,
        "max_daily_cap": settings.application_linkedin_daily_cap,
        "ats_platform": _platform_from_strategy(strategy.value),
    }
    submitter_kwargs = _filter_submitter_kwargs(submitter, candidate_kwargs)

    try:
        return await submitter(**submitter_kwargs)
    except Exception as exc:
        logger.exception(
            "application.handler_crashed",
            dedup_key=listing.dedup_key,
            strategy=strategy.value,
        )

        screenshot_path = ""
        if page is not None:
            screenshot_path = await capture_application_failure(
                page,
                listing,
                run_id,
                f"{strategy.value}_node",
                "Submitter crashed",
                error=str(exc),
            )

        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="error",
            strategy=strategy.value,
            summary=f"Submitter crashed: {exc}",
            errors=[str(exc)],
            screenshot_path=screenshot_path,
        )


def _filter_submitter_kwargs(submitter: Any, candidate: dict[str, Any]) -> dict[str, Any]:
    """Return only the kwargs the submitter's signature actually accepts."""
    sig = inspect.signature(submitter)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return candidate
    return {k: v for k, v in candidate.items() if k in sig.parameters}


def _apply_status_transition(
    state: JobAgentState,
    listing: JobListing,
    attempt: ApplicationAttempt,
) -> None:
    """Update listing/application state based on attempt outcome."""
    if attempt.status == "submitted":
        listing.status = ApplicationStatus.APPLIED
        listing.applied_at = datetime.now(UTC)
        return

    if attempt.status == "awaiting_review":
        listing.status = ApplicationStatus.PENDING_REVIEW
        return

    listing.status = ApplicationStatus.ERRORED
    state.errors.append(
        {
            "node": "application",
            "dedup_key": listing.dedup_key,
            "message": attempt.summary[:500],
        }
    )
