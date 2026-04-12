"""Run discovery node as a standalone executable script.

Usage examples:
    python scripts/run_discovery_node.py --sources linkedin --keywords "platform engineer"
    python scripts/run_discovery_node.py --sources greenhouse,lever --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.observability import setup_logging  # noqa: E402
from pipelines.job_agent.models import JobSource, SearchCriteria  # noqa: E402
from pipelines.job_agent.nodes.discovery import discovery_node  # noqa: E402
from pipelines.job_agent.state import JobAgentState  # noqa: E402


def _parse_sources(raw: str) -> list[JobSource]:
    if not raw.strip():
        return []
    parsed: list[JobSource] = []
    for token in [t.strip().lower() for t in raw.split(",") if t.strip()]:
        try:
            parsed.append(JobSource(token))
        except ValueError as exc:
            valid = ", ".join(sorted(s.value for s in JobSource))
            msg = f"Unknown source '{token}'. Valid values: {valid}"
            raise ValueError(msg) from exc
    return parsed


def _split_csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _default_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"discovery-{stamp}-{uuid.uuid4().hex[:8]}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run standalone discovery node.")
    parser.add_argument("--keywords", default="platform engineer,backend engineer")
    parser.add_argument("--roles", default="staff engineer,senior software engineer")
    parser.add_argument("--companies", default="")
    parser.add_argument("--locations", default="United States")
    parser.add_argument("--sources", default="", help="Comma-separated JobSource values.")
    parser.add_argument("--salary-floor", type=int, default=0)
    parser.add_argument("--remote-ok", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--run-id", default="")
    parser.add_argument(
        "--write-json",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Write discovered listings to data/discovery_runs/<run_id>.json",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    sources = _parse_sources(args.sources)
    criteria = SearchCriteria(
        keywords=_split_csv(args.keywords),
        target_roles=_split_csv(args.roles),
        target_companies=_split_csv(args.companies),
        locations=_split_csv(args.locations),
        salary_floor=args.salary_floor,
        remote_ok=args.remote_ok,
        sources=sources,
    )
    run_id = args.run_id.strip() or _default_run_id()

    state = JobAgentState(
        search_criteria=criteria,
        run_id=run_id,
        dry_run=args.dry_run,
    )
    out = await discovery_node(state)

    print(f"run_id={run_id}")
    print(f"phase={out.phase.value}")
    print(f"discovered={len(out.discovered_listings)}")
    print(f"errors={len(out.errors)}")
    if out.errors:
        print("error_details:")
        for idx, err in enumerate(out.errors, start=1):
            node = err.get("node", "")
            provider = err.get("provider", "")
            msg = err.get("message", "")
            print(f"  {idx:>2}. node={node} provider={provider} message={msg}")
    for idx, listing in enumerate(out.discovered_listings[:20], start=1):
        print(
            f"{idx:>2}. [{listing.source.value}] {listing.company} | "
            f"{listing.title} | {listing.url}"
        )

    if args.write_json:
        out_dir = _REPO_ROOT / "data" / "discovery_runs"
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": run_id,
            "dry_run": args.dry_run,
            "criteria": {
                "keywords": criteria.keywords,
                "target_roles": criteria.target_roles,
                "target_companies": criteria.target_companies,
                "locations": criteria.locations,
                "salary_floor": criteria.salary_floor,
                "remote_ok": criteria.remote_ok,
                "sources": [s.value for s in criteria.sources],
            },
            "errors": out.errors,
            "listings": [
                {
                    "dedup_key": li.dedup_key,
                    "title": li.title,
                    "company": li.company,
                    "location": li.location,
                    "url": li.url,
                    "source": li.source.value,
                    "salary_min": li.salary_min,
                    "salary_max": li.salary_max,
                    "status": li.status.value,
                }
                for li in out.discovered_listings
            ],
        }
        output_path = out_dir / f"{run_id}.json"
        output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"wrote_json={output_path}")

    return 0 if not out.errors else 1


def main() -> None:
    setup_logging()
    parser = _build_parser()
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
