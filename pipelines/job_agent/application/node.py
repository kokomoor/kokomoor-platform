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

    # Lazily create the LLM client the same way every other LLM-backed node
    # does. Without this, Easy Apply fields that the deterministic field_mapper
    # can't fill with confidence ≥ 0.8 are silently left blank — required
    # fields in the wizard go unanswered.
    if llm_client is None:
        from core.llm import AnthropicClient
        llm_client = AnthropicClient()

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

    # 2. Browser Submissions — mirrors DiscoveryOrchestrator's auth pattern
    #    exactly: try with loaded session, invalidate + retry fresh on failure.
    if browser_listings:
        session_store = SessionStore(Path("data/sessions"))
        batch_results, batch_pairs = await _run_browser_batch(
            browser_listings=browser_listings,
            profile=profile,
            llm_client=llm_client,
            settings=settings,
            run_id=state.run_id,
            dry_run=state.dry_run,
            cache=cache,
            session_store=session_store,
            log=log,
        )
        for listing, attempt in batch_pairs:
            attempts.append(attempt)
            attempt_pairs.append((listing, attempt))
            await _handle_attempt_outcome(state, listing, attempt, dedup_store)

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


async def _run_browser_batch(
    *,
    browser_listings: list[tuple[Any, SubmissionStrategy]],
    profile: Any,
    llm_client: Any,
    settings: Settings,
    run_id: str,
    dry_run: bool,
    cache: Any,
    session_store: SessionStore,
    log: Any,
) -> tuple[list[ApplicationAttempt], list[tuple[Any, ApplicationAttempt]]]:
    """Run browser submissions using the same auth pattern as DiscoveryOrchestrator.

    Tries with the loaded LinkedIn session first. If authentication fails,
    invalidates the session and retries with a clean browser context — exactly
    the two-attempt pattern the discovery phase uses. This ensures the
    application engine never gets stuck behind a stale or captcha-flagged
    session without a recovery path.
    """
    has_linkedin = any(
        s == SubmissionStrategy.TEMPLATE_LINKEDIN_EASY_APPLY for _, s in browser_listings
    )

    storage = session_store.load("linkedin") if has_linkedin else None
    had_session = storage is not None

    # Each element of this list is (storage_state_to_try, is_retry).
    # We try the loaded session first; if LinkedIn auth fails we wipe the
    # session file and try once more with a fresh context (storage_state=None).
    candidates: list[dict[str, Any] | None] = [storage]
    if had_session:
        candidates.append(None)  # retry slot

    for attempt_idx, storage_state in enumerate(candidates):
        is_retry = attempt_idx > 0
        async with BrowserManager(storage_state=storage_state) as manager:
            # Authenticate using LinkedInProvider — the same code path
            # that discovery uses so we get all auth modes (welcome-back,
            # password-only, full login, captcha pause).
            if has_linkedin:
                auth_ok = await _authenticate_linkedin(manager, settings, log)
            else:
                auth_ok = True  # non-LinkedIn browser providers skip auth

            if not auth_ok:
                if had_session and not is_retry:
                    # First attempt failed: wipe poisoned session, let the
                    # loop run again with a fresh context.
                    session_store.invalidate("linkedin")
                    log.info("application.linkedin_session_invalidated_retrying_fresh")
                    continue
                # Either no session to invalidate, or the fresh-context
                # attempt also failed. Mark every LinkedIn listing stuck.
                log.warning(
                    "application.linkedin_auth_failed_all_attempts",
                    had_session=had_session,
                )
                stuck: list[ApplicationAttempt] = []
                pairs: list[tuple[Any, ApplicationAttempt]] = []
                for listing, strategy in browser_listings:
                    if strategy == SubmissionStrategy.TEMPLATE_LINKEDIN_EASY_APPLY:
                        a = ApplicationAttempt(
                            dedup_key=listing.dedup_key,
                            status="stuck",
                            strategy=strategy.value,
                            summary=(
                                "LinkedIn authentication failed on both session and "
                                "fresh-context attempts. Resolve the login (solve "
                                "captcha in headed mode or refresh credentials) and "
                                "retry."
                            ),
                        )
                        stuck.append(a)
                        pairs.append((listing, a))
                return stuck, pairs

            # Auth succeeded — run the batch.
            results: list[ApplicationAttempt] = []
            result_pairs: list[tuple[Any, ApplicationAttempt]] = []
            for index, (listing, strategy) in enumerate(browser_listings):
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
                        run_id=run_id,
                        dry_run=dry_run,
                        cache=cache,
                    )
                finally:
                    await page.close()

                results.append(attempt)
                result_pairs.append((listing, attempt))
                await _jittered_delay(index, len(browser_listings), dry_run, settings)

            # Persist the refreshed session so the next run starts clean.
            if has_linkedin:
                try:
                    await session_store.save("linkedin", manager)
                except Exception:
                    log.warning("application.session_save_failed", exc_info=True)

            return results, result_pairs

    # Should be unreachable, but satisfy type checker.
    return [], []


async def _authenticate_linkedin(
    manager: BrowserManager,
    settings: Settings,
    log: Any,
) -> bool:
    """Authenticate LinkedIn within an open BrowserManager context.

    Navigates to www.linkedin.com and delegates to LinkedInProvider —
    the same provider used by DiscoveryOrchestrator — so all auth modes
    (welcome-back card, password-only, full login) are covered without
    duplicating logic.

    After provider.authenticate() returns True, this function verifies
    that the LinkedIn session token (li_at) is actually present in the
    browser context. LinkedIn's SPA router can navigate to /feed/ based
    on cached client-side state without the server issuing li_at, causing
    is_authenticated to return a false positive. Without li_at, subsequent
    job-view pages show the unauthenticated "Sign in to see who you know"
    modal instead of the Easy Apply button.

    If li_at is missing, we force a full server-side login by navigating
    directly to /login — which triggers a real HTTP redirect to /feed/
    only when the server validates credentials, guaranteeing li_at is set.
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

        # LinkedInProvider.authenticate() already checks is_authenticated
        # internally and short-circuits if the session is already valid,
        # so calling it unconditionally is safe and idempotent.
        success = await provider.authenticate(
            page,
            email=email,
            password=password,
            behavior=behavior,
        )

        if not success:
            log.warning("application.linkedin_auth_failed")
            return False

        # --- li_at verification gate ---
        # Confirm the LinkedIn session token is actually present in the
        # browser context. Without this check, a stale or captcha-flagged
        # session can produce a false "authenticated" state: LinkedIn's SPA
        # navigates to /feed/ via client-side routing (without a server-side
        # auth exchange), is_authenticated sees the /feed/ URL and returns
        # True, but li_at is never set so job-view pages render as guest.
        cookies = await manager.get_cookies(["https://www.linkedin.com"])
        li_at_present = any(c.get("name") == "li_at" for c in cookies)

        if not li_at_present:
            log.warning(
                "application.linkedin_auth_ok_but_no_li_at_forcing_full_login",
                page_url=page.url,
            )
            # Force a server-side auth exchange by navigating to /login.
            # The server redirects to /feed/ and issues li_at only when
            # credentials are genuinely validated — no SPA false positive.
            try:
                await page.goto(
                    "https://www.linkedin.com/login",
                    wait_until="domcontentloaded",
                    timeout=15_000,
                )
            except Exception:
                log.warning("application.linkedin_login_nav_failed", exc_info=True)
                return False
            await behavior.reading_pause(800)
            success = await provider.authenticate(
                page,
                email=email,
                password=password,
                behavior=behavior,
            )
            if not success:
                log.warning("application.linkedin_forced_login_failed")
                return False
            # Final verification — if li_at is still missing after a full
            # credential entry the account needs manual intervention
            # (captcha, verification email, etc.).
            cookies = await manager.get_cookies(["https://www.linkedin.com"])
            li_at_present = any(c.get("name") == "li_at" for c in cookies)
            if not li_at_present:
                log.error("application.linkedin_li_at_missing_after_forced_login")
                return False

        log.info("application.linkedin_auth_ok", page_url=page.url)
        return True
    except Exception:
        log.exception("application.linkedin_auth_crashed")
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
