"""Run the truncated manual pipeline for a single job URL.

Usage:
    python scripts/run_manual_url_tailor.py "https://example.com/jobs/123"
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.observability import setup_logging  # noqa: E402
from pipelines.job_agent.graph import build_manual_graph  # noqa: E402
from pipelines.job_agent.models import SearchCriteria  # noqa: E402
from pipelines.job_agent.state import JobAgentState, PipelinePhase  # noqa: E402


async def main() -> None:
    setup_logging()

    if len(sys.argv) < 2:
        print('Usage: python scripts/run_manual_url_tailor.py "https://.../job"', file=sys.stderr)
        sys.exit(2)

    job_url = sys.argv[1].strip()
    state = JobAgentState(
        search_criteria=SearchCriteria(),
        manual_job_url=job_url,
        run_id="manual-url-run",
        dry_run=False,
    )

    graph = build_manual_graph()
    out = await graph.ainvoke(state)
    if isinstance(out, dict):
        errors = out.get("errors", [])
        qualified = out.get("qualified_listings", [])
        phase = out.get("phase")
        if errors:
            print("Errors:", errors, file=sys.stderr)
            sys.exit(1)
        if not qualified:
            print("No listing extracted from URL", file=sys.stderr)
            sys.exit(1)
        listing = qualified[0]
        print("Phase:", phase)
        print("Extracted:", listing.company, "-", listing.title)
        print("Source:", listing.source.value)
        print("Dedup key:", listing.dedup_key)
        print("Tailored resume:", listing.tailored_resume_path)
        return

    if out.errors:
        print("Errors:", out.errors, file=sys.stderr)
        sys.exit(1)

    if not out.qualified_listings:
        print("No listing extracted from URL", file=sys.stderr)
        sys.exit(1)

    listing = out.qualified_listings[0]
    print("Phase:", out.phase.value if isinstance(out.phase, PipelinePhase) else out.phase)
    print("Extracted:", listing.company, "-", listing.title)
    print("Source:", listing.source.value)
    print("Dedup key:", listing.dedup_key)
    print("Tailored resume:", listing.tailored_resume_path)


if __name__ == "__main__":
    asyncio.run(main())
