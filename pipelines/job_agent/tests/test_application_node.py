"""Tests for the application orchestrator node (Prompt 05 scope)."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from core.config import get_settings
from pipelines.job_agent.graph import build_graph, build_manual_graph
from pipelines.job_agent.models import (
    ApplicationAttempt,
    ApplicationStatus,
    JobListing,
    SearchCriteria,
)
from pipelines.job_agent.state import JobAgentState

_EXAMPLE_PROFILE = (
    Path(__file__).resolve().parents[1] / "context" / "candidate_application.example.yaml"
)


@pytest.fixture(autouse=True)
def _stub_resume_artifact() -> Path:
    path = Path("/tmp/resume.pdf")
    path.write_bytes(b"%PDF-1.4 stub")
    return path


def _set_application_env(monkeypatch: pytest.MonkeyPatch, *, max_per_run: int = 5) -> None:
    import tempfile
    monkeypatch.setenv("KP_CANDIDATE_APPLICATION_PROFILE_PATH", str(_EXAMPLE_PROFILE))
    monkeypatch.setenv("KP_APPLICATION_MAX_PER_RUN", str(max_per_run))
    monkeypatch.setenv("KP_APPLICATION_REQUIRE_HUMAN_REVIEW", "true")
    monkeypatch.setenv("KP_APPLICATION_MIN_DELAY_SECONDS", "10")
    monkeypatch.setenv("KP_APPLICATION_DEDUP_DB_PATH", tempfile.mktemp(suffix=".db"))
    get_settings.cache_clear()


def _listing(
    *,
    dedup_key: str,
    url: str,
    status: ApplicationStatus = ApplicationStatus.DISCOVERED,
    resume_path: Path | str = "/tmp/resume.pdf",
) -> JobListing:
    return JobListing(
        title="Software Engineer",
        company="Example Co",
        location="Remote",
        url=url,
        dedup_key=dedup_key,
        status=status,
        tailored_resume_path=str(resume_path),
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


@pytest.mark.asyncio
async def test_error_status_is_logged_with_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the 2026-04-14 run produced 3 opaque application errors
    because the submitter returned status='error' via ApplicationAttempt
    rather than raising, and the summary string was never logged."""
    from pipelines.job_agent.application import node as node_module
    from pipelines.job_agent.application.node import application_node

    _set_application_env(monkeypatch)

    async def _stub_submit(*args: object, **kwargs: object) -> ApplicationAttempt:
        _ = args
        listing = cast("JobListing", kwargs.get("listing") or args[0])
        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="error",
            strategy="api_greenhouse",
            summary="Easy Apply button not found on page.",
            screenshot_path="/tmp/snap.png",
        )

    monkeypatch.setattr(
        "pipelines.job_agent.application.node.get_submitter",
        lambda _: _stub_submit,
    )

    captured: list[tuple[str, dict[str, object]]] = []

    def _capture(event: str, **kwargs: object) -> None:
        captured.append((event, kwargs))

    original_warning = node_module.logger.warning
    monkeypatch.setattr(
        node_module.logger,
        "warning",
        lambda event, **kwargs: (_capture(event, **kwargs), original_warning(event, **kwargs))[1],
    )

    state = JobAgentState(
        search_criteria=SearchCriteria(),
        run_id="test-app-error-log",
        tailored_listings=[
            _listing(dedup_key="gh-err-1", url="https://boards.greenhouse.io/acme/jobs/999"),
        ],
    )

    out = await application_node(state)

    assert len(out.application_results) == 1
    assert out.application_results[0].status == "error"

    error_events = [
        kwargs for event, kwargs in captured if event == "application.attempt_errored"
    ]
    assert error_events, "Expected application.attempt_errored log event"
    payload = error_events[0]
    assert payload["dedup_key"] == "gh-err-1"
    assert payload["run_id"] == "test-app-error-log"
    assert payload["strategy"] == "api_greenhouse"
    assert "Easy Apply button not found" in str(payload["summary"])


@pytest.mark.asyncio
async def test_stuck_status_is_logged_at_info_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stuck attempts (external redirects, daily cap, etc.) should log at
    INFO so operators see them in the normal log stream but they don't
    trigger the error-count metric."""
    from pipelines.job_agent.application import node as node_module
    from pipelines.job_agent.application.node import application_node

    _set_application_env(monkeypatch)

    async def _stub_submit(*args: object, **kwargs: object) -> ApplicationAttempt:
        _ = args
        listing = cast("JobListing", kwargs.get("listing") or args[0])
        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="stuck",
            strategy="template_linkedin_easy_apply",
            summary=(
                "LinkedIn listing has an 'Apply' button (external "
                "redirect), not 'Easy Apply'."
            ),
        )

    monkeypatch.setattr(
        "pipelines.job_agent.application.node.get_submitter",
        lambda _: _stub_submit,
    )

    async def _warmup_ok(manager: object, settings: object, log: object) -> bool:
        return True

    monkeypatch.setattr(node_module, "_authenticate_linkedin", _warmup_ok)

    captured: list[tuple[str, dict[str, object]]] = []

    original_info = node_module.logger.info
    monkeypatch.setattr(
        node_module.logger,
        "info",
        lambda event, **kwargs: (captured.append((event, kwargs)), original_info(event, **kwargs))[1],
    )

    state = JobAgentState(
        search_criteria=SearchCriteria(),
        run_id="test-app-stuck-log",
        tailored_listings=[
            _listing(
                dedup_key="li-stuck-1",
                url="https://www.linkedin.com/jobs/view/12345/",
            ),
        ],
    )

    await application_node(state)

    stuck_events = [
        kwargs for event, kwargs in captured if event == "application.attempt_stuck"
    ]
    assert stuck_events, "Expected application.attempt_stuck log event"
    payload = stuck_events[0]
    assert payload["dedup_key"] == "li-stuck-1"
    assert "external" in str(payload["summary"]).lower()


@pytest.mark.asyncio
async def test_linkedin_auth_warmup_failure_short_circuits_listings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the 2026-04-14 run hit three opaque
    ``Easy Apply button not found`` errors because discovery had
    saved a captcha-flagged session, the application engine loaded
    it, navigated straight to ``/jobs/view/<id>``, and got back the
    logged-out public render. Now the application node must warm up
    LinkedIn auth before touching any job URL and, when that warmup
    fails, mark LinkedIn listings as ``stuck`` and invalidate the
    poisoned session instead of invoking the submitter."""
    from pipelines.job_agent.application import node as node_module
    from pipelines.job_agent.application.node import application_node

    _set_application_env(monkeypatch)

    submitter_called = False

    async def _stub_submit(*args: object, **kwargs: object) -> ApplicationAttempt:
        nonlocal submitter_called
        submitter_called = True
        raise AssertionError(
            "Submitter must not run when auth warmup fails."
        )

    monkeypatch.setattr(
        "pipelines.job_agent.application.node.get_submitter",
        lambda _: _stub_submit,
    )

    async def _warmup_fail(
        manager: object, settings: object, log: object
    ) -> bool:
        return False

    monkeypatch.setattr(node_module, "_authenticate_linkedin", _warmup_fail)

    class _FakeBrowserManager:
        async def __aenter__(self) -> _FakeBrowserManager:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def new_page(self) -> object:
            raise AssertionError("new_page must not be called when warmup fails")

    monkeypatch.setattr(
        node_module,
        "BrowserManager",
        lambda storage_state=None: _FakeBrowserManager(),
    )

    invalidated: list[str] = []
    saved: list[str] = []

    class _FakeSessionStore:
        def __init__(self, _path: object) -> None:
            pass

        def load(self, _source: str) -> dict[str, object] | None:
            return {"cookies": [{"fake": "poisoned"}]}

        def invalidate(self, source: str) -> None:
            invalidated.append(source)

        async def save(self, source: str, _manager: object) -> bool:
            saved.append(source)
            return True

    monkeypatch.setattr(node_module, "SessionStore", _FakeSessionStore)

    state = JobAgentState(
        search_criteria=SearchCriteria(),
        run_id="test-linkedin-auth-warmup-fail",
        tailored_listings=[
            _listing(
                dedup_key="li-warmup-1",
                url="https://www.linkedin.com/jobs/view/4399700465/",
            ),
            _listing(
                dedup_key="li-warmup-2",
                url="https://www.linkedin.com/jobs/view/4401567323/",
            ),
        ],
    )

    out = await application_node(state)

    assert submitter_called is False
    assert len(out.application_results) == 2
    for result in out.application_results:
        assert result.status == "stuck"
        assert result.strategy == "template_linkedin_easy_apply"
        assert "authentication failed" in result.summary.lower()

    assert invalidated == ["linkedin"], (
        "Poisoned session must be invalidated when auth fails."
    )
    assert saved == [], "Session must not be saved when auth fails."


@pytest.mark.asyncio
async def test_linkedin_auth_warmup_success_runs_submitter_and_saves_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive case: when auth warmup returns True, the submitter
    runs normally and the refreshed session is persisted after the
    batch so the next run starts from a known-good state."""
    from pipelines.job_agent.application import node as node_module
    from pipelines.job_agent.application.node import application_node

    _set_application_env(monkeypatch)

    submit_calls: list[str] = []

    async def _stub_submit(*args: object, **kwargs: object) -> ApplicationAttempt:
        _ = args
        listing = cast("JobListing", kwargs.get("listing") or args[0])
        submit_calls.append(listing.dedup_key)
        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="awaiting_review",
            strategy="template_linkedin_easy_apply",
            summary="ready for review",
        )

    monkeypatch.setattr(
        "pipelines.job_agent.application.node.get_submitter",
        lambda _: _stub_submit,
    )

    async def _warmup_ok(
        manager: object, settings: object, log: object
    ) -> bool:
        return True

    monkeypatch.setattr(node_module, "_authenticate_linkedin", _warmup_ok)

    class _FakePage:
        async def close(self) -> None:
            return None

    class _FakeBrowserManager:
        async def __aenter__(self) -> _FakeBrowserManager:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def new_page(self) -> _FakePage:
            return _FakePage()

    monkeypatch.setattr(
        node_module,
        "BrowserManager",
        lambda storage_state=None: _FakeBrowserManager(),
    )

    saved: list[str] = []
    invalidated: list[str] = []

    class _FakeSessionStore:
        def __init__(self, _path: object) -> None:
            pass

        def load(self, _source: str) -> dict[str, object] | None:
            return {"cookies": []}

        def invalidate(self, source: str) -> None:
            invalidated.append(source)

        async def save(self, source: str, _manager: object) -> bool:
            saved.append(source)
            return True

    monkeypatch.setattr(node_module, "SessionStore", _FakeSessionStore)

    state = JobAgentState(
        search_criteria=SearchCriteria(),
        run_id="test-linkedin-auth-warmup-ok",
        tailored_listings=[
            _listing(
                dedup_key="li-warmup-ok-1",
                url="https://www.linkedin.com/jobs/view/4399700465/",
            ),
        ],
    )

    out = await application_node(state)

    assert submit_calls == ["li-warmup-ok-1"]
    assert len(out.application_results) == 1
    assert out.application_results[0].status == "awaiting_review"
    assert saved == ["linkedin"], "Session must be saved after a successful batch."
    assert invalidated == []


@pytest.mark.asyncio
async def test_authenticate_linkedin_forces_login_when_li_at_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: provider.authenticate can return True based purely on the
    /feed/ URL shortcut in is_authenticated without li_at being set in the
    browser context (LinkedIn SPA navigates client-side without server auth).
    _authenticate_linkedin must detect the missing li_at and force a full
    login through /login, which triggers a real server-side auth exchange."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from pipelines.job_agent.application.node import _authenticate_linkedin

    settings = MagicMock()
    settings.linkedin_email = "test@example.com"
    settings.linkedin_password.get_secret_value.return_value = "password123"

    log = MagicMock()
    log.warning = MagicMock()
    log.info = MagicMock()
    log.error = MagicMock()

    page = MagicMock()
    page.goto = AsyncMock()
    page.url = "https://www.linkedin.com/feed/"
    page.close = AsyncMock()

    # First get_cookies call: no li_at (SPA false positive)
    # Second get_cookies call (after forced /login): li_at present
    manager = MagicMock()
    manager.new_page = AsyncMock(return_value=page)
    manager.get_cookies = AsyncMock(side_effect=[
        [],  # first call: no li_at — SPA false positive
        [{"name": "li_at", "value": "real-session-token"}],  # after forced /login
    ])

    authenticate_calls: list[str] = []

    async def _fake_authenticate(page: object, *, email: str, password: str, behavior: object) -> bool:
        authenticate_calls.append("called")
        return True

    # HumanBehavior's reading_pause is awaited — make it an AsyncMock.
    mock_behavior = MagicMock()
    mock_behavior.reading_pause = AsyncMock()

    with patch(
        "pipelines.job_agent.discovery.providers.linkedin.LinkedInProvider",
    ) as _mock_cls, patch(
        "pipelines.job_agent.application.node.HumanBehavior",
        return_value=mock_behavior,
    ):
        _mock_cls.return_value.base_domain.return_value = "www.linkedin.com"
        _mock_cls.return_value.authenticate = _fake_authenticate
        result = await _authenticate_linkedin(manager, settings, log)

    assert result is True
    # authenticate must be called twice: initial + forced /login path
    assert len(authenticate_calls) == 2
    log.warning.assert_any_call(
        "application.linkedin_auth_ok_but_no_li_at_forcing_full_login",
        page_url=page.url,
    )
    log.info.assert_called_with("application.linkedin_auth_ok", page_url=page.url)


@pytest.mark.asyncio
async def test_authenticate_linkedin_fails_when_li_at_missing_after_forced_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the forced /login path also fails to produce li_at, auth must
    return False so _run_browser_batch can invalidate the session and retry."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from pipelines.job_agent.application.node import _authenticate_linkedin

    settings = MagicMock()
    settings.linkedin_email = "test@example.com"
    settings.linkedin_password.get_secret_value.return_value = "password123"

    log = MagicMock()
    log.warning = MagicMock()
    log.error = MagicMock()

    page = MagicMock()
    page.goto = AsyncMock()
    page.url = "https://www.linkedin.com/feed/"
    page.close = AsyncMock()

    manager = MagicMock()
    manager.new_page = AsyncMock(return_value=page)
    # Both calls return no li_at: forced login also failed to produce a session
    manager.get_cookies = AsyncMock(return_value=[])

    mock_behavior = MagicMock()
    mock_behavior.reading_pause = AsyncMock()

    with patch(
        "pipelines.job_agent.discovery.providers.linkedin.LinkedInProvider",
    ) as _mock_cls, patch(
        "pipelines.job_agent.application.node.HumanBehavior",
        return_value=mock_behavior,
    ):
        _mock_cls.return_value.base_domain.return_value = "www.linkedin.com"
        _mock_cls.return_value.authenticate = AsyncMock(return_value=True)
        result = await _authenticate_linkedin(manager, settings, log)

    assert result is False
    log.error.assert_called_with("application.linkedin_li_at_missing_after_forced_login")


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
