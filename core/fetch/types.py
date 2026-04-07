"""Shared types for HTTP and browser content fetching."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FetchMethod(StrEnum):
    """How HTML was obtained."""

    HTTP = "http"
    BROWSER = "browser"


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Outcome of a single fetch: final URL after redirects and response body."""

    html: str
    final_url: str
    status_code: int
    method: FetchMethod
