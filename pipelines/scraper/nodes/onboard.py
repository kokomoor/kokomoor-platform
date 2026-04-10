"""LLM-driven site onboarding node.

When a user wants to add a new scraping target, this node:

1. Navigates the target site with browser actions to explore its structure
   (auth flow, search mechanics, result pages, pagination).
2. Feeds the exploration data to a flagship LLM which generates a
   ``SiteProfile`` describing the site.
3. Captures an initial fixture set (multiple page variants).
4. Presents extracted sample records for human verification as golden records.

The output is everything needed to scrape the site going forward:
a profile YAML, fixtures, golden records, and optionally a wrapper
subclass skeleton if the site has unusual quirks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from core.browser import BrowserManager
from core.browser.actions import BrowserActions
from core.browser.human_behavior import HumanBehavior
from core.scraper.fixtures import FixtureStore, compute_fingerprint
from core.scraper.path_safety import validate_site_id

if TYPE_CHECKING:
    from core.llm.protocol import LLMClient

logger = structlog.get_logger(__name__)

_ONBOARD_SYSTEM_PROMPT = """You are an expert web scraper engineer. Analyze the provided web page
snapshots and produce a complete SiteProfile JSON for scraping this site.

The SiteProfile must include:
- site_id: unique slug
- display_name: human-readable name
- base_url: site root
- auth: authentication requirements (type, selectors, env var keys)
- rate_limit: conservative delays appropriate for this type of site
- requires_browser: whether JavaScript rendering is needed
- navigation: search URL template, pagination strategy, selectors
- selectors: result_item selector, field_map (output field -> CSS selector)
- output_contract: fields (name, type, required), dedup_fields, min_records_per_search
- notes: quirks, constraints, observations

Be conservative with rate limits — government and public record sites should
use min_delay_s >= 5.0 and long pauses. Commercial sites can be slightly faster.

Return ONLY valid JSON matching the SiteProfile schema. No markdown, no explanation."""


async def onboard_site(
    target_url: str,
    site_description: str,
    *,
    llm: LLMClient,
    fixture_store: FixtureStore | None = None,
    profiles_dir: str | Path = "pipelines/scraper/profiles",
    max_exploration_pages: int = 5,
) -> dict[str, Any]:
    """Explore a target site and generate a SiteProfile.

    Args:
        target_url: Starting URL of the site to onboard.
        site_description: User's description of what they want to scrape.
        llm: LLM client for profile generation.
        fixture_store: Where to save captured fixtures.
        profiles_dir: Where to save the generated profile YAML.
        max_exploration_pages: How many pages to explore.

    Returns:
        Dict with keys: profile (dict), fixture_path (str), sample_records (list).
    """
    fixture_store = fixture_store or FixtureStore()
    page_snapshots: list[dict[str, Any]] = []

    async with BrowserManager() as browser:
        page = await browser.new_page()
        actions = BrowserActions(page, HumanBehavior())

        nav = await actions.goto(target_url, timeout_ms=30_000)
        if not nav.success:
            logger.error("onboard.navigation_failed", url=target_url, error=nav.error)
            return {"error": f"Cannot reach {target_url}: {nav.error}"}

        for i in range(max_exploration_pages):
            html = await page.content()
            title = await page.title()
            url = page.url
            fp = compute_fingerprint(html)

            snapshot = {
                "page_index": i,
                "url": url,
                "title": title,
                "fingerprint": fp.to_dict(),
                "html_preview": html[:3000],
                "interactive_elements": fp.interactive_element_count,
                "form_fields": fp.form_fields[:20],
                "key_classes": fp.key_classes[:30],
                "key_ids": fp.key_ids[:20],
            }
            page_snapshots.append(snapshot)

            links = await page.evaluate("""
                () => Array.from(document.querySelectorAll('a[href]'))
                    .map(a => ({text: a.textContent?.trim()?.slice(0, 50), href: a.href}))
                    .filter(l => l.href.startsWith(window.location.origin))
                    .slice(0, 20)
            """)
            snapshot["internal_links"] = links

            if i < max_exploration_pages - 1:
                search_link = _find_search_link(links)
                if search_link:
                    await actions.goto(search_link)
                else:
                    break

        capture_dir = await _capture_exploration_fixtures(
            fixture_store,
            target_url,
            page,
            page_snapshots,
        )

    profile_dict = await _generate_profile(llm, target_url, site_description, page_snapshots)

    if profile_dict:
        _save_profile_yaml(profile_dict, profiles_dir)

    return {
        "profile": profile_dict,
        "fixture_path": str(capture_dir) if capture_dir else "",
        "page_snapshots_count": len(page_snapshots),
        "sample_fields": _extract_sample_fields(page_snapshots),
    }


def _find_search_link(links: list[dict[str, str]]) -> str | None:
    """Heuristic: find a link that looks like a search page."""
    search_terms = ["search", "find", "lookup", "query", "browse"]
    for link in links:
        text = (link.get("text") or "").lower()
        href = (link.get("href") or "").lower()
        if any(t in text or t in href for t in search_terms):
            return link.get("href")
    return None


async def _capture_exploration_fixtures(
    store: FixtureStore,
    target_url: str,
    page: Any,
    snapshots: list[dict[str, Any]],
) -> Path | None:
    """Capture fixtures from the exploration pages."""
    from urllib.parse import urlparse

    parsed = urlparse(target_url)
    site_slug = parsed.netloc.replace(".", "_").replace("-", "_")

    pages_html: list[tuple[str, str, str]] = []
    for snap in snapshots:
        label = f"page_{snap['page_index']:03d}"
        pages_html.append((label, snap["url"], snap.get("html_preview", "")))

    if pages_html:
        return await store.capture_pages(site_slug, pages_html)
    return None


async def _generate_profile(
    llm: LLMClient,
    target_url: str,
    description: str,
    snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Use LLM to generate a SiteProfile from exploration data."""
    sanitized_snapshots = []
    for snap in snapshots:
        sanitized_snapshots.append(
            {
                "url": snap["url"],
                "title": snap.get("title", ""),
                "form_fields": snap.get("form_fields", []),
                "key_classes": snap.get("key_classes", []),
                "key_ids": snap.get("key_ids", []),
                "internal_links": snap.get("internal_links", [])[:10],
                "interactive_elements": snap.get("interactive_elements", 0),
            }
        )

    user_prompt = f"""Target URL: {target_url}
User description: {description}

Exploration data ({len(snapshots)} pages visited):
{json.dumps(sanitized_snapshots, indent=2, default=str)}

Generate a complete SiteProfile JSON for this site."""

    try:
        response_text: str = await llm.complete(
            prompt=user_prompt,
            system=_ONBOARD_SYSTEM_PROMPT,
            max_tokens=4096,
        )
        text = response_text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(text)  # type: ignore[no-any-return]
    except (json.JSONDecodeError, Exception) as exc:
        logger.error("onboard.profile_generation_failed", error=str(exc)[:300])
        return {}


def _save_profile_yaml(profile: dict[str, Any], profiles_dir: str | Path) -> Path:
    """Save the generated profile as a YAML file."""
    import yaml

    profiles_path = Path(profiles_dir)
    profiles_path.mkdir(parents=True, exist_ok=True)

    site_id = validate_site_id(str(profile.get("site_id", "unknown_site")))
    filepath = profiles_path / f"{site_id}.yaml"
    filepath.write_text(
        yaml.dump(profile, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    logger.info("onboard.profile_saved", path=str(filepath), site_id=site_id)
    return filepath


def _extract_sample_fields(snapshots: list[dict[str, Any]]) -> list[str]:
    """Extract a sample of detected field names from exploration."""
    all_fields: set[str] = set()
    for snap in snapshots:
        all_fields.update(snap.get("form_fields", []))
    return sorted(all_fields)[:20]
