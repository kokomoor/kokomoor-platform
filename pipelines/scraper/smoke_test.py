"""Live smoke test CLI for the scraper pipeline.

Runs a quick scrape against a target site to verify the wrapper and
profile are working correctly, then validates the results.

Usage::

    python -m pipelines.scraper.smoke_test --site linkedin --query "python developer"
    python -m pipelines.scraper.smoke_test --site vision_gsi_woonsocketri --max-pages 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import structlog

from core.config import get_settings
from core.scraper.content_store import ContentStore
from core.scraper.dedup import DedupEngine
from core.scraper.fixtures import FixtureStore
from pipelines.scraper.models import ScrapeRequest, SiteProfile
from pipelines.scraper.nodes.scrape import scrape_node
from pipelines.scraper.nodes.validate import validate_result

logger = structlog.get_logger(__name__)


def _load_profile(site_id: str) -> SiteProfile:
    """Load a site profile from the profiles directory."""
    import yaml

    settings = get_settings()
    profiles_dir = Path(settings.scraper_profiles_dir)

    for path in profiles_dir.glob("*.yaml"):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if data and data.get("site_id") == site_id:
                return SiteProfile(**data)
        except Exception:
            continue

    msg = f"Profile not found for site_id={site_id!r} in {profiles_dir}"
    raise FileNotFoundError(msg)


async def run_smoke_test(
    site_id: str,
    *,
    search_params: dict[str, str] | None = None,
    max_records: int = 10,
    max_pages: int = 2,
    dedup_enabled: bool = False,
    persist_records: bool = False,
) -> dict[str, object]:
    """Run a smoke test against a single site.

    Returns a summary dict with results and timing.
    """
    t_start = time.monotonic()

    profile = _load_profile(site_id)
    settings = get_settings()

    dedup = None
    if dedup_enabled:
        dedup = DedupEngine(settings.scraper_dedup_db_path)

    content_store = ContentStore(settings.scraper_content_dir) if persist_records else None
    fixture_store = FixtureStore(settings.scraper_fixtures_dir)

    request = ScrapeRequest(
        site_id=site_id,
        search_params=search_params or {},
        max_records=max_records,
        max_pages=max_pages,
    )

    print(f"\n{'=' * 60}")
    print(f"Smoke Test: {profile.display_name or site_id}")
    print(f"URL: {profile.base_url}")
    print(f"Max records: {max_records} | Max pages: {max_pages}")
    print(f"Browser required: {profile.requires_browser}")
    print(f"{'=' * 60}\n")

    result = await scrape_node(
        request,
        profile,
        dedup=dedup,
        content_store=content_store,
        fixture_store=fixture_store,
    )

    report = validate_result(result, profile, fixture_store=fixture_store)

    elapsed = time.monotonic() - t_start

    print(f"\n{'=' * 60}")
    print(f"Results: {len(result.records)} records extracted")
    print(f"Pages visited: {result.pages_visited}")
    print(f"Errors: {len(result.errors)}")
    print(f"Warnings: {len(result.warnings)}")
    print(f"Drift detected: {result.drift_detected}")
    if result.fingerprint_similarity is not None:
        print(f"Fingerprint similarity: {result.fingerprint_similarity:.2%}")
    print("\nTiming:")
    print(f"  Auth:     {result.timing.auth_ms:>8.1f} ms")
    print(f"  Search:   {result.timing.search_ms:>8.1f} ms")
    print(f"  Extract:  {result.timing.extract_ms:>8.1f} ms")
    print(f"  Paginate: {result.timing.paginate_ms:>8.1f} ms")
    print(f"  Dedup:    {result.timing.dedup_ms:>8.1f} ms")
    print(f"  Total:    {elapsed * 1000:>8.1f} ms")
    print(f"\nValidation: {report.summary}")

    if result.records:
        print("\nSample records (first 3):")
        for rec in result.records[:3]:
            print(f"  {json.dumps(rec, default=str)[:200]}")

    if result.errors:
        print("\nErrors:")
        for err in result.errors:
            print(f"  [{err.classification}] {err.message[:150]}")

    if result.warnings:
        print("\nWarnings:")
        for warn in result.warnings:
            print(f"  {warn[:150]}")

    print(f"{'=' * 60}\n")

    if dedup:
        dedup.close()

    return {
        "site_id": site_id,
        "records": len(result.records),
        "pages": result.pages_visited,
        "errors": len(result.errors),
        "drift": result.drift_detected,
        "validation_passed": report.passed,
        "elapsed_s": round(elapsed, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Scraper smoke test CLI")
    parser.add_argument("--site", required=True, help="site_id to test")
    parser.add_argument("--query", default="", help="Search query")
    parser.add_argument("--max-records", type=int, default=10)
    parser.add_argument("--max-pages", type=int, default=2)
    parser.add_argument("--dedup", action="store_true")
    parser.add_argument("--persist", action="store_true")
    args = parser.parse_args()

    search_params: dict[str, str] = {}
    if args.query:
        search_params["query"] = args.query

    result = asyncio.run(
        run_smoke_test(
            args.site,
            search_params=search_params,
            max_records=args.max_records,
            max_pages=args.max_pages,
            dedup_enabled=args.dedup,
            persist_records=args.persist,
        )
    )

    sys.exit(0 if result.get("validation_passed") else 1)


if __name__ == "__main__":
    main()
