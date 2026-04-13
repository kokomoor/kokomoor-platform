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
        async with BrowserManager(storage_state=session_store.load("linkedin")) as manager:
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
