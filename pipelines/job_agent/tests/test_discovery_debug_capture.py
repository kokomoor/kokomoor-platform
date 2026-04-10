"""Tests for discovery failure-capture diagnostics."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipelines.job_agent.discovery.debug_capture import FailureCapture
from pipelines.job_agent.models import JobSource


class _FakePage:
    url = "https://example.com/login"

    async def title(self) -> str:
        return "Login - Example"

    async def screenshot(self, *, path: str, full_page: bool) -> None:
        Path(path).write_bytes(b"png")

    async def content(self) -> str:
        return "<html><body>login</body></html>"


@pytest.mark.asyncio
async def test_capture_page_failure_writes_artifacts(tmp_path: Path) -> None:
    capture = FailureCapture(
        enabled=True,
        base_dir=str(tmp_path),
        run_id="run-123",
        include_html=True,
    )

    artifacts = await capture.capture_page_failure(
        source=JobSource.LINKEDIN,
        stage="auth_failed",
        reason="provider_authenticate_returned_false",
        page=_FakePage(),  # type: ignore[arg-type]
    )

    assert artifacts
    # metadata + screenshot + html
    assert any(path.endswith("metadata.json") for path in artifacts)
    assert any(path.endswith("page.png") for path in artifacts)
    assert any(path.endswith("page.html") for path in artifacts)


def test_capture_metadata_failure_writes_file(tmp_path: Path) -> None:
    capture = FailureCapture(
        enabled=True,
        base_dir=str(tmp_path),
        run_id="run-123",
        include_html=True,
    )
    artifacts = capture.capture_metadata_failure(
        source=JobSource.GREENHOUSE,
        stage="http_provider_exception",
        reason="provider_runner_failed",
        error="404",
    )
    assert len(artifacts) == 1
    assert artifacts[0].endswith("metadata.json")
