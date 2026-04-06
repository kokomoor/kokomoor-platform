"""Reusable web fetch primitives for all pipelines (HTTP, browser, JSON-LD).

Pipeline-specific scrapers should depend on ``ContentFetcher`` and compose
``HttpFetcher`` / ``BrowserFetcher`` rather than duplicating httpx or Playwright
setup.
"""

from __future__ import annotations

from core.fetch.browser_fetch import BrowserFetcher
from core.fetch.http_client import HttpFetcher
from core.fetch.jsonld import (
    iter_json_ld_objects_from_html,
    iter_json_ld_objects_from_soup,
    parse_json_ld_payload,
)
from core.fetch.protocol import ContentFetcher
from core.fetch.types import FetchMethod, FetchResult

__all__ = [
    "BrowserFetcher",
    "ContentFetcher",
    "FetchMethod",
    "FetchResult",
    "HttpFetcher",
    "iter_json_ld_objects_from_html",
    "iter_json_ld_objects_from_soup",
    "parse_json_ld_payload",
]
