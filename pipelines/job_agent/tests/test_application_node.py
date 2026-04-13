"""Tests for the application orchestrator node (Prompt 05 scope)."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from core.config import get_settings
from pipelines.job_agent.models import ApplicationAttempt
from pipelines.job_agent.graph import build_graph, build_manual_graph
from pipelines.job_agent.models import ApplicationStatus, JobListing, SearchCriteria
from pipelines.job_agent.state import JobAgentState

_EXAMPLE_PROFILE = (
    Path(__file__).resolve().parents[1] / "context" / "candidate_application.example.yaml"
)


def _set_application_env(monkeypatch: pytest.MonkeyPatch, *, max_per_run: int = 5) -> None:
    import tempfile
    monkeypatch.setenv("KP_CANDIDATE_APPLICATION_PROFILE_PATH", str(_EXAMPLE_PROFILE))
    monkeypatch.setenv("KP_APPLICATION_MAX_PER_RUN", str(max_per_run))
    monkeypatch.setenv("KP_APPLICATION_REQUIRE_HUMAN_REVIEW", "true")
    monkeypatch.setenv("KP_APPLICATION_MIN_DELAY_SECONDS", "10")
    monkeypatch.setenv("KP_APPLICATION_DEDUP_DB_PATH", tempfile.mktemp(suffix=".db"))
    get_settings.cache_clear()


def _listing(
    *, dedup_key: str, url: str, status: ApplicationStatus = ApplicationStatus.DISCOVERED
) -> JobListing:
    return JobListing(
        title="Software Engineer",
        company="Example Co",
        location="Remote",
        url=url,
        dedup_key=dedup_key,
        status=status,
        tailored_resume_path="/tmp/resume.pdf",
    )


@pytest.mark.asyncio
async def test_application_node_dry_run_greenhouse_awaiting_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipelines.job_agent.application.node import application_node

    _set_application_env(monkeypatch)

    async def _stub_submit(*args: object, **kwargs: object) -> ApplicationAttempt:
        _ = args
        _ = kwargs
        return ApplicationAttempt(
            dedup_key="gh-1",
            status="awaiting_review",
            strategy="api_greenhouse",
            summary="dry run payload",
        )

    monkeypatch.setattr(
        "pipelines.job_agent.application.node.get_submitter",
        lambda _: _stub_submit,
    )

    state = JobAgentState(
        search_criteria=SearchCriteria(),
        run_id="test-app-dry",
        dry_run=True,
        tailored_listings=[
            _listing(dedup_key="gh-1", url="https://boards.greenhouse.io/acme/jobs/123"),
        ],
    )

    out = await application_node(state)
    assert len(out.application_results) == 1
    assert out.application_results[0].status == "awaiting_review"


@pytest.mark.asyncio
async def test_application_max_per_run_limits_processed_listings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipelines.job_agent.application.node import application_node

    _set_application_env(monkeypatch, max_per_run=1)

    calls: list[str] = []

    async def _stub_submit(*args: object, **kwargs: object) -> ApplicationAttempt:
        _ = args
        listing = cast("JobListing", kwargs.get("listing") or args[0])
        calls.append(listing.dedup_key)
        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="awaiting_review",
            strategy="api_greenhouse",
            summary="ok",
        )

    monkeypatch.setattr(
        "pipelines.job_agent.application.node.get_submitter",
        lambda _: _stub_submit,
    )

    state = JobAgentState(
        search_criteria=SearchCriteria(),
        run_id="test-app-cap",
        tailored_listings=[
            _listing(dedup_key="gh-1", url="https://boards.greenhouse.io/acme/jobs/123"),
            _listing(dedup_key="gh-2", url="https://boards.greenhouse.io/acme/jobs/456"),
        ],
    )

    out = await application_node(state)
    assert calls == ["gh-1"]
    assert len(out.application_results) == 1
    assert out.application_results[0].dedup_key == "gh-1"


@pytest.mark.asyncio
async def test_listing_already_errored_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    from pipelines.job_agent.application.node import application_node

    _set_application_env(monkeypatch)

    async def _stub_submit(*args: object, **kwargs: object) -> ApplicationAttempt:
        _ = args
        _ = kwargs
        raise AssertionError("submitter should not be called for pre-errored listings")

    monkeypatch.setattr(
        "pipelines.job_agent.application.node.get_submitter",
        lambda _: _stub_submit,
    )

    state = JobAgentState(
        search_criteria=SearchCriteria(),
        run_id="test-app-skip-errored",
        tailored_listings=[
            _listing(
                dedup_key="gh-1",
                url="https://boards.greenhouse.io/acme/jobs/123",
                status=ApplicationStatus.ERRORED,
            )
        ],
    )

    out = await application_node(state)
    assert out.application_results == []


@pytest.mark.asyncio
async def test_non_greenhouse_returns_stuck_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipelines.job_agent.application.node import application_node

    _set_application_env(monkeypatch)

    state = JobAgentState(
        search_criteria=SearchCriteria(),
        run_id="test-app-stuck",
        tailored_listings=[
            _listing(
                dedup_key="lever-1-unique",
                url="https://jobs.lever.co/acme/123e4567-e89b-12d3-a456-426614174000",
            )
        ],
    )

    out = await application_node(state)
    assert len(out.application_results) == 1
    assert out.application_results[0].status == "awaiting_review"
    assert out.application_results[0].strategy == "api_lever"


def test_graph_routes_cover_letter_to_application_before_tracking() -> None:
    graph = build_graph()
    graph_view = graph.get_graph()
    edges = {(edge.source, edge.target) for edge in graph_view.edges}

    assert ("cover_letter_tailoring", "application") in edges
    assert ("application", "tracking") in edges
    assert ("cover_letter_tailoring", "tracking") not in edges

    manual_graph = build_manual_graph()
    manual_edges = {(edge.source, edge.target) for edge in manual_graph.get_graph().edges}
    assert ("cover_letter_tailoring", "application") in manual_edges
    assert ("application", "tracking") in manual_edges
