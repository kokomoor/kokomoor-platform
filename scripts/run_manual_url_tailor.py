"""Run the truncated manual pipeline for a single job URL.

Fetches the job, runs analysis, tailors resume and cover letter.
Does not attempt to submit an application.

Usage:
    python scripts/run_manual_url_tailor.py "https://example.com/jobs/123"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.observability import setup_logging  # noqa: E402
from pipelines.job_agent.extraction.inspection import (  # noqa: E402
    write_extracted_job_markdown,
    write_job_analysis_markdown,
)
from pipelines.job_agent.graph import build_manual_tailor_graph  # noqa: E402
from pipelines.job_agent.models import JobListing, SearchCriteria  # noqa: E402
from pipelines.job_agent.state import JobAgentState, PipelinePhase  # noqa: E402

if TYPE_CHECKING:
    from pipelines.job_agent.models.resume_tailoring import JobAnalysisResult


def _print_results(
    listing: JobListing,
    extracted_path: Path,
    analysis_path: Path | None,
    errors: list[dict[str, str]],
) -> None:
    print(f"Extracted: {listing.company} - {listing.title}")
    print(f"Source:    {listing.source.value}")
    print(f"Dedup key: {listing.dedup_key}")
    print(f"Scraped job (markdown): {extracted_path}")
    if analysis_path:
        print(f"Job analysis (markdown): {analysis_path}")
    if listing.tailored_resume_path:
        print(f"Tailored resume (.docx): {listing.tailored_resume_path}")
    else:
        print("Tailored resume: NOT GENERATED", file=sys.stderr)
    if listing.tailored_cover_letter_path:
        print(f"Tailored cover letter (.docx): {listing.tailored_cover_letter_path}")
    else:
        cl_errors = [e for e in errors if e.get("node") == "cover_letter_tailoring"]
        if cl_errors:
            print(f"Tailored cover letter: FAILED — {cl_errors[0].get('message', '')[:120]}", file=sys.stderr)
        else:
            print("Tailored cover letter: NOT GENERATED", file=sys.stderr)


def _write_artifacts(
    listing: JobListing,
    analyses: dict[str, JobAnalysisResult],
    *,
    run_id: str,
) -> tuple[Path, Path | None]:
    extracted = write_extracted_job_markdown(listing, run_id=run_id)
    analysis_path: Path | None = None
    analysis = analyses.get(listing.dedup_key)
    if analysis is not None:
        analysis_path = write_job_analysis_markdown(listing, analysis, run_id=run_id)
    return extracted, analysis_path


def _default_run_id(job_url: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    nonce = uuid.uuid4().hex[:8]
    return f"manual-url-{stamp}-{nonce}"


def _coerce_state(out: JobAgentState | dict) -> JobAgentState:
    if isinstance(out, JobAgentState):
        return out
    from pipelines.job_agent.state import coerce_state
    return coerce_state(out)


async def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(
        description="Tailor resume and cover letter for a single job URL."
    )
    parser.add_argument("url", help="Job board URL to tailor for")
    parser.add_argument("--run-id", default="", help="Optional run identifier")
    parser.add_argument(
        "--older",
        action="store_true",
        help="Age-up mode: contour resume and cover letter to read as mid-to-senior career.",
    )
    args = parser.parse_args()

    job_url = args.url.strip()
    run_id = (
        args.run_id.strip()
        or os.getenv("KP_MANUAL_RUN_ID", "").strip()
        or _default_run_id(job_url)
    )
    state = JobAgentState(
        search_criteria=SearchCriteria(),
        manual_job_url=job_url,
        run_id=run_id,
        dry_run=False,
        age_up=args.older,
    )

    graph = build_manual_tailor_graph()
    raw_out = await graph.ainvoke(state)
    out = _coerce_state(raw_out)

    if not out.qualified_listings:
        print("No listing extracted from URL", file=sys.stderr)
        if out.errors:
            print("Errors:", out.errors, file=sys.stderr)
        sys.exit(1)

    listing = out.qualified_listings[0]

    # Resume is required; cover letter errors are reported but not fatal.
    resume_errors = [e for e in out.errors if e.get("node") != "cover_letter_tailoring"]
    if not listing.tailored_resume_path:
        print("Resume tailoring failed:", resume_errors or out.errors, file=sys.stderr)
        sys.exit(1)

    phase = out.phase.value if isinstance(out.phase, PipelinePhase) else out.phase
    print(f"Phase: {phase}")

    extracted, analysis_path = _write_artifacts(listing, out.job_analyses, run_id=run_id)
    _print_results(listing, extracted, analysis_path, out.errors)

    # Exit non-zero only if resume failed; cover-letter failures are logged above.
    cover_letter_failed = any(e.get("node") == "cover_letter_tailoring" for e in out.errors)
    sys.exit(1 if cover_letter_failed and not listing.tailored_cover_letter_path else 0)


if __name__ == "__main__":
    asyncio.run(main())
