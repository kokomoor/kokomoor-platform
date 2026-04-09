"""Human-readable artifacts for inspecting extractor and analysis output.

Two outputs:
1. **Extracted job markdown** — the full scraped ``JobListing.description``
   (what the scraper captured, no truncation).
2. **Analysis markdown** — the structured ``JobAnalysisResult`` that the
   tailoring node will use for plan generation.

Together these let a human verify the full pipeline handoff.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from core.config import get_settings

if TYPE_CHECKING:
    from pipelines.job_agent.models import JobListing
    from pipelines.job_agent.models.resume_tailoring import JobAnalysisResult


def write_extracted_job_markdown(
    listing: JobListing,
    *,
    run_id: str,
    output_root: Path | None = None,
) -> Path:
    """Write the full scraped job description to a Markdown file.

    This is the raw content the scraper captured — no truncation, no
    structured extraction — so a human can verify what was scraped.
    """
    root = output_root if output_root is not None else Path(get_settings().resume_output_dir)
    out_dir = root / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_id = listing.dedup_key[:16] if listing.dedup_key else "unknown"
    path = out_dir / f"extracted_job_{safe_id}.md"

    extraction_notes: dict[str, object] = {}
    if listing.notes:
        try:
            loaded = json.loads(listing.notes)
            if isinstance(loaded, dict):
                extraction_notes = loaded
        except json.JSONDecodeError:
            extraction_notes = {}

    raw_description = extraction_notes.get("raw_description")
    raw_section = ""
    if isinstance(raw_description, str) and raw_description.strip():
        raw_section = f"""
## Raw extracted block

{raw_description}
"""

    body = f"""# Scraped job content (full, untruncated)

**Title:** {listing.title}
**Company:** {listing.company}
**URL:** {listing.url}
**Description length:** {len(listing.description or "")} chars

---

{listing.description or "(empty)"}
{raw_section}
"""
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path


def write_job_analysis_markdown(
    listing: JobListing,
    analysis: JobAnalysisResult,
    *,
    run_id: str,
    output_root: Path | None = None,
) -> Path:
    """Write the structured analysis the tailoring node will consume.

    This is what the LLM produced from the full JD — the exact input
    the plan pass will use for resume tailoring decisions.
    """
    root = output_root if output_root is not None else Path(get_settings().resume_output_dir)
    out_dir = root / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_id = listing.dedup_key[:16] if listing.dedup_key else "unknown"
    path = out_dir / f"job_analysis_{safe_id}.md"

    themes = "\n".join(f"- {t}" for t in analysis.themes) or "- (none)"
    keywords = "\n".join(f"- {k}" for k in analysis.must_hit_keywords) or "- (none)"
    priority = "\n".join(f"- {r}" for r in analysis.priority_requirements) or "- (none)"
    basic_q = "\n".join(f"- {q}" for q in analysis.basic_qualifications) or "- (none)"
    pref_q = "\n".join(f"- {q}" for q in analysis.preferred_qualifications) or "- (none)"
    angles = "\n".join(f"- {a}" for a in analysis.angles) or "- (none)"
    domain = ", ".join(analysis.domain_tags) or "(none)"

    body = f"""# Job analysis (tailoring node input)

**Title:** {listing.title}
**Company:** {listing.company}
**Seniority:** {analysis.seniority}
**Domain tags:** {domain}

## Themes
{themes}

## Must-hit keywords (ATS)
{keywords}

## Priority requirements
{priority}

## Basic qualifications
{basic_q}

## Preferred qualifications
{pref_q}

## Positioning angles
{angles}
"""
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path
