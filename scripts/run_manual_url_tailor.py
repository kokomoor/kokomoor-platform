"""Run the truncated manual pipeline for a single job URL.

Usage:
    python scripts/run_manual_url_tailor.py "https://example.com/jobs/123"
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
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
from pipelines.job_agent.graph import build_manual_graph  # noqa: E402
from pipelines.job_agent.models import JobListing, SearchCriteria  # noqa: E402
from pipelines.job_agent.state import JobAgentState, PipelinePhase  # noqa: E402

if TYPE_CHECKING:
    from pipelines.job_agent.models.resume_tailoring import JobAnalysisResult


def _print_success(
    listing: JobListing,
    extracted_path: Path,
    analysis_path: Path | None,
) -> None:
    print("Extracted:", listing.company, "-", listing.title)
    print("Source:", listing.source.value)
    print("Dedup key:", listing.dedup_key)
    print("Scraped job (markdown):", extracted_path)
    if analysis_path:
        print("Job analysis (markdown):", analysis_path)
    print("Tailored resume (.docx):", listing.tailored_resume_path)


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
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    url_hash = hashlib.sha256(job_url.encode("utf-8")).hexdigest()[:8]
    return f"manual-url-{stamp}-{url_hash}"


async def main() -> None:
    setup_logging()

    if len(sys.argv) < 2:
        print(
            'Usage: python scripts/run_manual_url_tailor.py "https://.../job" [run-id]',
            file=sys.stderr,
        )
        sys.exit(2)

    job_url = sys.argv[1].strip()
    run_id = (
        (sys.argv[2].strip() if len(sys.argv) > 2 and sys.argv[2].strip() else "")
        or os.getenv("KP_MANUAL_RUN_ID", "").strip()
        or _default_run_id(job_url)
    )
    state = JobAgentState(
        search_criteria=SearchCriteria(),
        manual_job_url=job_url,
        run_id=run_id,
        dry_run=False,
    )

    graph = build_manual_graph()
    out = await graph.ainvoke(state)
    if isinstance(out, dict):
        errors = out.get("errors", [])
        qualified = out.get("qualified_listings", [])
        analyses = out.get("job_analyses", {})
        phase = out.get("phase")
        if errors:
            print("Errors:", errors, file=sys.stderr)
            sys.exit(1)
        if not qualified:
            print("No listing extracted from URL", file=sys.stderr)
            sys.exit(1)
        listing = qualified[0]
        print("Phase:", phase)
        extracted, analysis_path = _write_artifacts(listing, analyses, run_id=run_id)
        _print_success(listing, extracted, analysis_path)
        return

    if out.errors:
        print("Errors:", out.errors, file=sys.stderr)
        sys.exit(1)

    if not out.qualified_listings:
        print("No listing extracted from URL", file=sys.stderr)
        sys.exit(1)

    listing = out.qualified_listings[0]
    print("Phase:", out.phase.value if isinstance(out.phase, PipelinePhase) else out.phase)
    extracted, analysis_path = _write_artifacts(listing, out.job_analyses, run_id=run_id)
    _print_success(listing, extracted, analysis_path)


if __name__ == "__main__":
    asyncio.run(main())
