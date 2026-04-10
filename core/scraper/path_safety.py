"""Helpers for safe site-scoped filesystem paths and identifiers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_SITE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")


def validate_site_id(site_id: str) -> str:
    """Validate and normalize a site identifier used for partitioning.

    Site IDs are used in filesystem paths and SQLite partition names, so they
    must be constrained to a safe slug format.
    """
    candidate = site_id.strip()
    if candidate != site_id:
        msg = "Invalid site_id. Leading/trailing whitespace is not allowed."
        raise ValueError(msg)
    if not _SITE_ID_RE.fullmatch(candidate):
        msg = "Invalid site_id. Expected 1-63 chars matching ^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$."
        raise ValueError(msg)
    return candidate


def safe_join(base_dir: Path, site_id: str) -> Path:
    """Safely build a child path under ``base_dir`` for ``site_id``."""
    safe_site = validate_site_id(site_id)
    base = base_dir.resolve()
    candidate = (base / safe_site).resolve()
    if not candidate.is_relative_to(base):
        msg = f"Resolved site path escapes base dir: {candidate}"
        raise ValueError(msg)
    return candidate
