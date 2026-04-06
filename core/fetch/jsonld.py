"""Domain-agnostic helpers for ``application/ld+json`` script tags."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from bs4 import BeautifulSoup, Tag

if TYPE_CHECKING:
    from collections.abc import Iterator


def parse_json_ld_payload(payload: str) -> list[Any]:
    """Parse a single ``<script type=\"application/ld+json\">`` body into objects.

    Returns a flat list of top-level JSON values (object or primitive). If the
    payload is a JSON array, each element is returned as a separate item.
    """
    payload = payload.strip()
    if not payload:
        return []
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return list(data)
    return [data]


def iter_json_ld_objects_from_soup(soup: BeautifulSoup) -> Iterator[Any]:
    """Yield every JSON-LD object embedded in *soup* (including ``@graph`` items)."""
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not isinstance(script, Tag):
            continue
        inner = (script.string or script.get_text() or "").strip()
        if not inner:
            continue
        yield from parse_json_ld_payload(inner)


def iter_json_ld_objects_from_html(html: str) -> Iterator[Any]:
    """Parse *html* and yield JSON-LD objects (convenience when no soup exists yet)."""
    soup = BeautifulSoup(html, "html.parser")
    yield from iter_json_ld_objects_from_soup(soup)
