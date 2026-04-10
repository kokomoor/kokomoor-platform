"""URL canonicalization and shared helpers for discovery providers.

Tracking parameters add noise to dedup keys and reveal bot behavior to job boards.
Canonical URLs must be deterministic given the same job posting.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

if TYPE_CHECKING:
    from pipelines.job_agent.models import SearchCriteria

STRIP_QUERY_PREFIXES: tuple[str, ...] = (
    "utm_",
    "fbclid",
    "gclid",
    "trk",
    "mc_",
    "ref",
    "src",
    "via",
    "campaign",
)

KEEP_QUERY_KEYS: frozenset[str] = frozenset(
    {
        "gh_jid",
        "jk",
        "job",
        "jobid",
        "jid",
        "reqid",
        "lever-via",
        "token",
        "id",
    }
)

_LINKEDIN_JOB_ID_RE = re.compile(r"/jobs/(?:view/)?(\d+)")
_LINKEDIN_QUERY_ID_RE = re.compile(r"currentJobId=(\d+)")


def extract_job_id_from_linkedin_url(url: str) -> str | None:
    """Extract the numeric job ID from a LinkedIn URL."""
    m = _LINKEDIN_JOB_ID_RE.search(url)
    if m:
        return m.group(1)
    m = _LINKEDIN_QUERY_ID_RE.search(url)
    if m:
        return m.group(1)
    return None


def canonicalize_url(url: str) -> str:
    """Normalize a job URL for stable dedup and clean storage."""
    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()

    # --- Domain-specific rules ---
    if "linkedin.com" in netloc:
        job_id = extract_job_id_from_linkedin_url(url)
        if job_id:
            return f"{scheme}://{netloc}/jobs/view/{job_id}/"
        return urlunparse(parsed._replace(scheme=scheme, netloc=netloc, fragment="", query=""))

    if "indeed.com" in netloc:
        pairs = parse_qsl(parsed.query, keep_blank_values=False)
        jk_params = [(k, v) for k, v in pairs if k.lower() == "jk"]
        query = urlencode(sorted(jk_params))
        return urlunparse(parsed._replace(scheme=scheme, netloc=netloc, fragment="", query=query))

    if "greenhouse.io" in netloc:
        return urlunparse(parsed._replace(scheme=scheme, netloc=netloc, fragment="", query=""))

    if "lever.co" in netloc:
        return urlunparse(parsed._replace(scheme=scheme, netloc=netloc, fragment="", query=""))

    # --- Generic: keep job-identifying params, strip tracking ---
    pairs = parse_qsl(parsed.query, keep_blank_values=False)
    kept: list[tuple[str, str]] = []
    for key, value in pairs:
        key_lower = key.lower()
        if any(key_lower.startswith(prefix) for prefix in STRIP_QUERY_PREFIXES):
            continue
        if key_lower in KEEP_QUERY_KEYS:
            kept.append((key, value))
    query = urlencode(sorted(kept))
    return urlunparse(parsed._replace(scheme=scheme, netloc=netloc, fragment="", query=query))


def strip_tracking_params(url: str) -> str:
    """Alias for ``canonicalize_url`` for readability at call sites."""
    return canonicalize_url(url)


def matches_criteria(title: str, criteria: SearchCriteria) -> bool:
    """Return True if *title* matches any keyword or target role (or criteria is empty)."""
    if not criteria.keywords and not criteria.target_roles:
        return True
    title_lower = title.lower()
    return any(kw.lower() in title_lower for kw in criteria.keywords) or any(
        role.lower() in title_lower for role in criteria.target_roles
    )
