"""Protocol for pluggable fetch implementations (HTTP, browser, mocks, tests)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from core.fetch.types import FetchResult


@runtime_checkable
class ContentFetcher(Protocol):
    """Async fetch of a URL into HTML.

    Pipelines compose specialized extractors on top of one or more
    ``ContentFetcher`` implementations (e.g. HTTP first, browser fallback).
    """

    async def fetch(self, url: str) -> FetchResult:
        """Return HTML and the final URL after redirects."""
        ...
