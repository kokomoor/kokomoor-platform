"""Submitter registry — decouples the orchestrator from ATS implementations.

Provides a central registry where API submitters, template fillers, and
agent-based fillers register their handler functions. The orchestrator
calls ``get_submitter(strategy)`` to resolve a handler at runtime,
keeping the core graph logic clean and extensible.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

    import httpx
    from playwright.async_api import Page

    from core.llm.protocol import LLMClient
    from pipelines.job_agent.application.router import SubmissionStrategy
    from pipelines.job_agent.models import (
        ApplicationAttempt,
        CandidateApplicationProfile,
        JobListing,
    )


@runtime_checkable
class SubmitterHandler(Protocol):
    """Structural contract for an application submitter handler."""

    async def __call__(
        self,
        listing: JobListing,
        profile: CandidateApplicationProfile,
        resume_path: Path,
        cover_letter_path: Path | None,
        *,
        client: httpx.AsyncClient | None = None,
        page: Page | None = None,
        llm: LLMClient | None = None,
        run_id: str = "",
        dry_run: bool = True,
    ) -> ApplicationAttempt: ...


_REGISTRY: dict[SubmissionStrategy, SubmitterHandler] = {}


def register_submitter(strategy: SubmissionStrategy, handler: SubmitterHandler) -> None:
    """Register a handler for a specific submission strategy."""
    _REGISTRY[strategy] = handler


def get_submitter(strategy: SubmissionStrategy) -> SubmitterHandler | None:
    """Resolve a submitter handler from the registry."""
    return _REGISTRY.get(strategy)


def list_registered_strategies() -> list[SubmissionStrategy]:
    """Return all currently registered submission strategies."""
    return list(_REGISTRY.keys())
