"""One-off: tailor a resume for a single job URL.

Fetches the job description from the URL, runs job analysis, and produces
a tailored resume .docx. Does not generate a cover letter or attempt to apply.

Run from repo root with the venv active::

    python scripts/run_tailor_one.py "https://example.com/jobs/123"

Requires ``KP_ANTHROPIC_API_KEY`` and a master profile at
``KP_RESUME_MASTER_PROFILE_PATH`` (default: pipelines/job_agent/context/candidate_profile.yaml).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.observability import setup_logging  # noqa: E402
from pipelines.job_agent.models import SearchCriteria  # noqa: E402
from pipelines.job_agent.nodes.job_analysis import job_analysis_node  # noqa: E402
from pipelines.job_agent.nodes.manual_extraction import manual_extraction_node  # noqa: E402
from pipelines.job_agent.nodes.tailoring import tailoring_node  # noqa: E402
from pipelines.job_agent.state import JobAgentState  # noqa: E402


def _default_run_id(job_url: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    nonce = uuid.uuid4().hex[:8]
    return f"tailor-one-{stamp}-{nonce}"


async def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(description="Tailor a resume for a single job URL.")
    parser.add_argument("url", help="Job board URL to tailor for")
    parser.add_argument("--run-id", default="", help="Optional run identifier")
    parser.add_argument(
        "--older",
        action="store_true",
        help="Age-up mode: contour resume to read as mid-to-senior career.",
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

    state = await manual_extraction_node(state)
    if not state.qualified_listings:
        print("Extraction failed:", state.errors, file=sys.stderr)
        sys.exit(1)

    state = await job_analysis_node(state)
    if not state.job_analyses:
        print("Job analysis failed:", state.errors, file=sys.stderr)
        sys.exit(1)

    state = await tailoring_node(state)

    listing = state.qualified_listings[0]
    if not listing.tailored_resume_path:
        print("Tailoring failed:", state.errors, file=sys.stderr)
        sys.exit(1)

    print(f"Company:  {listing.company}")
    print(f"Title:    {listing.title}")
    print(f"Resume:   {listing.tailored_resume_path}")


if __name__ == "__main__":
    asyncio.run(main())
