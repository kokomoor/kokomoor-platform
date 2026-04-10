"""Run the full pipeline: discovery -> filtering -> extraction -> analysis
-> tailoring -> cover letter -> tracking -> notification.

Processes up to --num-jobs listings end-to-end (default 3).

Usage examples:
    # Defaults: TPM in Boston, 3 jobs, LinkedIn
    python scripts/run_discovery_to_pipeline_one.py

    # Senior TPM anywhere, 3 jobs
    python scripts/run_discovery_to_pipeline_one.py \
        --roles "senior technical product manager" \
        --keywords "technical product manager,TPM" \
        --locations "United States" \
        --num-jobs 3

    # Backend engineer, 5 jobs, force-process filtered listings
    python scripts/run_discovery_to_pipeline_one.py \
        --roles "senior backend engineer,staff engineer" \
        --keywords "backend engineer,platform engineer" \
        --num-jobs 5 --force-process-when-filtered
"""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.config import get_settings
from core.observability import setup_logging
from pipelines.job_agent.models import JobSource, SearchCriteria
from pipelines.job_agent.nodes.bulk_extraction import bulk_extraction_node
from pipelines.job_agent.nodes.cover_letter_tailoring import cover_letter_tailoring_node
from pipelines.job_agent.nodes.discovery import discovery_node
from pipelines.job_agent.nodes.filtering import filtering_node
from pipelines.job_agent.nodes.job_analysis import job_analysis_node
from pipelines.job_agent.nodes.notification import notification_node
from pipelines.job_agent.nodes.tailoring import tailoring_node
from pipelines.job_agent.nodes.tracking import tracking_node
from pipelines.job_agent.state import JobAgentState


def _parse_sources(raw: str) -> list[JobSource]:
    if not raw.strip():
        return []
    return [JobSource(t.strip().lower()) for t in raw.split(",") if t.strip()]


def _split_csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _default_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"pipeline-{stamp}-{uuid.uuid4().hex[:8]}"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Full pipeline: discover jobs, then process N listings end-to-end.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    search = p.add_argument_group("search criteria")
    search.add_argument(
        "--keywords",
        default="technical product manager,TPM",
        help="Comma-separated search keywords (default: TPM variants)",
    )
    search.add_argument(
        "--roles",
        default="technical product manager,senior technical product manager",
        help="Comma-separated target roles (default: TPM roles)",
    )
    search.add_argument(
        "--companies",
        default="",
        help="Comma-separated target companies (default: any)",
    )
    search.add_argument(
        "--locations",
        default="Boston",
        help="Comma-separated locations (default: Boston)",
    )
    search.add_argument(
        "--sources",
        default="linkedin",
        help="Comma-separated job sources (default: linkedin)",
    )
    search.add_argument(
        "--salary-floor",
        type=int,
        default=0,
        help="Minimum salary filter, 0 = no filter (default: 0)",
    )
    search.add_argument(
        "--remote-ok",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include remote positions (default: yes)",
    )

    pipeline = p.add_argument_group("pipeline control")
    pipeline.add_argument(
        "--num-jobs",
        type=int,
        default=3,
        help="Number of discovered jobs to process through full pipeline (default: 3)",
    )
    pipeline.add_argument(
        "--run-id",
        default="",
        help="Custom run ID (default: auto-generated)",
    )
    pipeline.add_argument(
        "--force-process-when-filtered",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Continue with listings even if filtering rejects them (default: yes)",
    )
    pipeline.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Discovery only, no LLM calls (default: no)",
    )

    return p


def _assert_runtime_prereqs(*, dry_run: bool) -> None:
    settings = get_settings()
    if not dry_run:
        if not settings.has_anthropic_key:
            raise RuntimeError(
                "KP_ANTHROPIC_API_KEY is required for analysis/tailoring nodes. "
                "Use --dry-run to skip LLM stages."
            )
        profile_path = Path(settings.resume_master_profile_path)
        if not profile_path.is_file():
            raise RuntimeError(
                f"KP_RESUME_MASTER_PROFILE_PATH does not exist: {profile_path}. "
                "Copy candidate_profile.example.yaml and point this var at it."
            )


def _print_header(label: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"{'=' * 60}")


def _print_listing(i: int, listing: object) -> None:
    src = getattr(listing, "source", "?")
    src_val = src.value if hasattr(src, "value") else src
    print(f"  [{i}] [{src_val}] {listing.company} | {listing.title}")  # type: ignore[attr-defined]
    print(f"      {listing.url}")  # type: ignore[attr-defined]


async def _run(args: argparse.Namespace) -> int:
    _assert_runtime_prereqs(dry_run=args.dry_run)

    criteria = SearchCriteria(
        keywords=_split_csv(args.keywords),
        target_roles=_split_csv(args.roles),
        target_companies=_split_csv(args.companies),
        locations=_split_csv(args.locations),
        salary_floor=args.salary_floor,
        remote_ok=args.remote_ok,
        sources=_parse_sources(args.sources),
    )
    run_id = args.run_id.strip() or _default_run_id()
    num_jobs: int = args.num_jobs

    state = JobAgentState(
        run_id=run_id,
        search_criteria=criteria,
        dry_run=args.dry_run,
    )

    # -- Discovery --
    _print_header("DISCOVERY")
    print(f"  keywords:  {criteria.keywords}")
    print(f"  roles:     {criteria.target_roles}")
    print(f"  locations: {criteria.locations}")
    print(f"  sources:   {[s.value for s in criteria.sources]}")
    state = await discovery_node(state)
    total = len(state.discovered_listings)
    print(f"\n  discovered={total}  errors={len(state.errors)}")

    if not state.discovered_listings:
        print("  No listings discovered. Check sources/criteria/env and retry.")
        return 1

    if args.dry_run:
        _print_header("DRY RUN COMPLETE")
        for i, listing in enumerate(state.discovered_listings[:num_jobs]):
            _print_listing(i, listing)
        print(f"\n  run_id={run_id}")
        return 0

    # -- Select top N --
    selected = state.discovered_listings[:num_jobs]
    _print_header(f"SELECTED {len(selected)} / {total} LISTINGS")
    for i, listing in enumerate(selected):
        _print_listing(i, listing)

    state.discovered_listings = selected
    state.qualified_listings = []
    state.job_analyses = {}
    state.errors = []

    # -- Filtering --
    _print_header("FILTERING")
    state = await filtering_node(state)
    print(
        f"  qualified={len(state.qualified_listings)}  filtered={len(selected) - len(state.qualified_listings)}"
    )

    if not state.qualified_listings:
        if args.force_process_when_filtered:
            state.qualified_listings = list(selected)
            print("  force_process=on -> continuing with all selected listings")
        else:
            print("  All listings filtered out. Use --force-process-when-filtered.")
            return 1
    elif len(state.qualified_listings) < len(selected) and args.force_process_when_filtered:
        missing = [li for li in selected if li not in state.qualified_listings]
        state.qualified_listings.extend(missing)
        print(f"  force_process=on -> added {len(missing)} filtered listings back")

    # -- Bulk Extraction --
    _print_header("BULK EXTRACTION")
    state = await bulk_extraction_node(state)
    extracted = sum(1 for li in state.qualified_listings if li.description)
    print(f"  extracted={extracted}/{len(state.qualified_listings)}  errors={len(state.errors)}")

    extractable = [li for li in state.qualified_listings if li.description]
    if not extractable:
        print("  No descriptions extracted; cannot run analysis/tailoring.")
        return 1
    state.qualified_listings = extractable

    # -- Job Analysis --
    _print_header("JOB ANALYSIS")
    state = await job_analysis_node(state)
    print(f"  analyses={len(state.job_analyses)}  errors={len(state.errors)}")

    if not state.job_analyses:
        print("  Job analysis failed for all listings; stopping.")
        return 1

    # -- Resume Tailoring --
    _print_header("RESUME TAILORING")
    state = await tailoring_node(state)
    resumes = sum(1 for li in state.qualified_listings if li.tailored_resume_path)
    print(f"  resumes_generated={resumes}/{len(state.qualified_listings)}")

    # -- Cover Letter Tailoring --
    _print_header("COVER LETTER TAILORING")
    state = await cover_letter_tailoring_node(state)
    covers = sum(1 for li in state.qualified_listings if li.tailored_cover_letter_path)
    print(f"  cover_letters_generated={covers}/{len(state.qualified_listings)}")

    # -- Tracking + Notification --
    _print_header("TRACKING & NOTIFICATION")
    state = await tracking_node(state)
    state = await notification_node(state)
    print(f"  phase={state.phase.value}")

    # -- Summary --
    _print_header("PIPELINE COMPLETE")
    print(f"  run_id:  {run_id}")
    print(f"  jobs:    {len(state.qualified_listings)}")
    print(f"  resumes: {resumes}")
    print(f"  covers:  {covers}")
    print(f"  errors:  {len(state.errors)}")

    for i, listing in enumerate(state.qualified_listings):
        print(f"\n  --- Listing {i + 1} ---")
        _print_listing(i, listing)
        print(f"      resume:       {listing.tailored_resume_path or 'FAILED'}")
        print(f"      cover_letter: {listing.tailored_cover_letter_path or 'FAILED'}")

    if state.errors:
        print(f"\n  Errors ({len(state.errors)}):")
        for err in state.errors:
            print(f"    - {err}")

    return 0 if not state.errors else 1


def main() -> None:
    setup_logging()
    parser = _build_parser()
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
