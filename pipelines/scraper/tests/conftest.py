"""Shared test fixtures for the scraper pipeline.

Provides helpers for:
- Loading offline HTML fixtures by site_id
- Creating mock SiteProfiles for testing
- Creating DedupEngine instances backed by temp directories
- Creating fixture stores backed by temp directories
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from core.scraper.content_store import ContentStore
from core.scraper.dedup import DedupEngine
from core.scraper.fixtures import FixtureStore

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path
from pipelines.scraper.models import (
    AuthConfig,
    FieldSpec,
    NavigationConfig,
    OutputContract,
    PaginationStrategy,
    RateLimitConfig,
    SelectorConfig,
    SiteProfile,
)


@pytest.fixture()
def tmp_dedup(tmp_path: Path) -> Generator[DedupEngine, None, None]:
    """DedupEngine backed by a temporary SQLite database."""
    db_path = tmp_path / "test_dedup.db"
    engine = DedupEngine(db_path, bloom_expected=10_000, bloom_fp_rate=0.01)
    yield engine
    engine.close()


@pytest.fixture()
def tmp_content_store(tmp_path: Path) -> ContentStore:
    """ContentStore backed by a temp directory."""
    return ContentStore(tmp_path / "content")


@pytest.fixture()
def tmp_fixture_store(tmp_path: Path) -> FixtureStore:
    """FixtureStore backed by a temp directory."""
    return FixtureStore(tmp_path / "fixtures")


@pytest.fixture()
def sample_output_contract() -> OutputContract:
    """A minimal output contract for test sites."""
    return OutputContract(
        fields=[
            FieldSpec(name="title", type="str", required=True),
            FieldSpec(name="url", type="url", required=True),
            FieldSpec(name="description", type="str", required=False),
            FieldSpec(name="price", type="str", required=False),
        ],
        dedup_fields=["title", "url"],
        min_records_per_search=1,
        max_empty_pages_before_stop=3,
    )


@pytest.fixture()
def sample_selector_config() -> SelectorConfig:
    """Selectors that work with ``SAMPLE_LISTING_HTML``."""
    return SelectorConfig(
        result_item=".listing-card",
        field_map={
            "title": "h3.title",
            "url": "a.link",
            "description": "p.desc",
            "price": "span.price",
        },
    )


@pytest.fixture()
def sample_profile(
    sample_output_contract: OutputContract,
    sample_selector_config: SelectorConfig,
) -> SiteProfile:
    """A complete SiteProfile for testing."""
    return SiteProfile(
        site_id="test_site",
        display_name="Test Site",
        base_url="https://test.example.com",
        auth=AuthConfig(),
        rate_limit=RateLimitConfig(min_delay_s=0.01, max_delay_s=0.02),
        requires_browser=True,
        navigation=NavigationConfig(
            search_url_template="https://test.example.com/search?q={query}&page={page}",
            pagination=PaginationStrategy.URL_PARAMETER,
        ),
        selectors=sample_selector_config,
        output_contract=sample_output_contract,
        fixture_refresh_days=7,
        max_pages_per_search=3,
    )


SAMPLE_LISTING_HTML = """
<html>
<body>
<div class="results">
  <div class="listing-card">
    <h3 class="title">Widget A</h3>
    <a class="link" href="/items/1">View</a>
    <p class="desc">A fine widget</p>
    <span class="price">$10.00</span>
  </div>
  <div class="listing-card">
    <h3 class="title">Widget B</h3>
    <a class="link" href="/items/2">View</a>
    <p class="desc">Another widget</p>
    <span class="price">$20.00</span>
  </div>
  <div class="listing-card">
    <h3 class="title">Widget C</h3>
    <a class="link" href="/items/3">View</a>
    <p class="desc">Yet another</p>
    <span class="price">$30.00</span>
  </div>
</div>
</body>
</html>
"""

SAMPLE_LISTING_HTML_DRIFTED = """
<html>
<body>
<div class="search-results-v2">
  <article class="listing-card redesigned">
    <h2 class="product-title">Widget A</h2>
    <a class="product-link" href="/items/1">View</a>
    <div class="product-info">
      <p class="desc">A fine widget</p>
      <span class="amount">$10.00</span>
    </div>
    <input type="hidden" name="csrf_token" value="abc123">
  </article>
</div>
</body>
</html>
"""

SAMPLE_GOLDEN_RECORDS: list[dict[str, Any]] = [
    {
        "title": "Widget A",
        "url": "https://test.example.com/items/1",
        "description": "A fine widget",
        "price": "$10.00",
    },
    {
        "title": "Widget B",
        "url": "https://test.example.com/items/2",
        "description": "Another widget",
        "price": "$20.00",
    },
    {
        "title": "Widget C",
        "url": "https://test.example.com/items/3",
        "description": "Yet another",
        "price": "$30.00",
    },
]
