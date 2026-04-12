"""Tests for core.scraper.fixtures — fingerprinting, drift detection, fixture store."""

from __future__ import annotations

from core.scraper.fixtures import (
    FixtureStore,
    StructuralFingerprint,
    compare_fingerprints,
    compute_fingerprint,
)
from pipelines.scraper.tests.conftest import (
    SAMPLE_LISTING_HTML,
    SAMPLE_LISTING_HTML_DRIFTED,
)


class TestComputeFingerprint:
    def test_returns_fingerprint(self) -> None:
        fp = compute_fingerprint(SAMPLE_LISTING_HTML)
        assert fp.tag_tree_hash
        assert fp.total_tags > 0

    def test_same_html_same_hash(self) -> None:
        fp1 = compute_fingerprint(SAMPLE_LISTING_HTML)
        fp2 = compute_fingerprint(SAMPLE_LISTING_HTML)
        assert fp1.tag_tree_hash == fp2.tag_tree_hash

    def test_different_html_different_hash(self) -> None:
        fp1 = compute_fingerprint(SAMPLE_LISTING_HTML)
        fp2 = compute_fingerprint(SAMPLE_LISTING_HTML_DRIFTED)
        assert fp1.tag_tree_hash != fp2.tag_tree_hash

    def test_detects_form_fields(self) -> None:
        html = '<html><body><input name="search" type="text"><select name="sort"></select></body></html>'
        fp = compute_fingerprint(html)
        assert len(fp.form_fields) == 2
        assert fp.interactive_element_count == 2

    def test_detects_key_classes(self) -> None:
        fp = compute_fingerprint(SAMPLE_LISTING_HTML)
        assert "listing-card" in fp.key_classes

    def test_ignores_text_content(self) -> None:
        html_a = '<html><body><div class="card"><p>Text A</p></div></body></html>'
        html_b = '<html><body><div class="card"><p>Text B</p></div></body></html>'
        fp_a = compute_fingerprint(html_a)
        fp_b = compute_fingerprint(html_b)
        assert fp_a.tag_tree_hash == fp_b.tag_tree_hash


class TestCompareFingerprints:
    def test_identical_is_1(self) -> None:
        fp = compute_fingerprint(SAMPLE_LISTING_HTML)
        drift = compare_fingerprints(fp, fp)
        assert drift.similarity == 1.0
        assert not drift.drifted
        assert drift.severity == "none"

    def test_drifted_is_below_threshold(self) -> None:
        fp_old = compute_fingerprint(SAMPLE_LISTING_HTML)
        fp_new = compute_fingerprint(SAMPLE_LISTING_HTML_DRIFTED)
        drift = compare_fingerprints(fp_old, fp_new, threshold=0.85)
        assert drift.similarity < 0.85
        assert drift.drifted
        assert drift.severity in ("low", "medium", "high")

    def test_reports_class_changes(self) -> None:
        fp_old = compute_fingerprint(SAMPLE_LISTING_HTML)
        fp_new = compute_fingerprint(SAMPLE_LISTING_HTML_DRIFTED)
        drift = compare_fingerprints(fp_old, fp_new)
        assert len(drift.removed_classes) > 0 or len(drift.added_classes) > 0

    def test_reports_form_field_changes(self) -> None:
        old_html = '<html><body><input name="q"></body></html>'
        new_html = '<html><body><input name="search"><input name="filter"></body></html>'
        fp_old = compute_fingerprint(old_html)
        fp_new = compute_fingerprint(new_html)
        drift = compare_fingerprints(fp_old, fp_new)
        assert len(drift.added_fields) > 0 or len(drift.removed_fields) > 0


class TestStructuralFingerprintSerialization:
    def test_roundtrip(self) -> None:
        fp = compute_fingerprint(SAMPLE_LISTING_HTML)
        data = fp.to_dict()
        restored = StructuralFingerprint.from_dict(data)
        assert restored.tag_tree_hash == fp.tag_tree_hash
        assert restored.form_fields == fp.form_fields
        assert restored.key_classes == fp.key_classes


class TestFixtureStore:
    def test_capture_and_load(self, tmp_fixture_store: FixtureStore) -> None:
        pages = [("page_001", "https://test.com/search", SAMPLE_LISTING_HTML)]
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            tmp_fixture_store.capture_pages("test_site", pages)
        )

        html = tmp_fixture_store.load_fixture_html("test_site", "page_001")
        assert html is not None
        assert "Widget A" in html

    def test_load_all_fixtures(self, tmp_fixture_store: FixtureStore) -> None:
        pages = [
            ("page_001", "https://test.com/1", "<html>1</html>"),
            ("page_002", "https://test.com/2", "<html>2</html>"),
        ]
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            tmp_fixture_store.capture_pages("test_site", pages)
        )

        all_fixtures = tmp_fixture_store.load_all_fixtures("test_site")
        assert len(all_fixtures) == 2

    def test_load_fingerprint(self, tmp_fixture_store: FixtureStore) -> None:
        pages = [("page_001", "https://test.com/search", SAMPLE_LISTING_HTML)]
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            tmp_fixture_store.capture_pages("test_site", pages)
        )

        fp = tmp_fixture_store.load_fingerprint("test_site")
        assert fp is not None
        assert fp.tag_tree_hash

    def test_golden_records(self, tmp_fixture_store: FixtureStore) -> None:
        pages = [("page_001", "https://test.com/search", "<html></html>")]
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            tmp_fixture_store.capture_pages("test_site", pages)
        )

        golden = [{"title": "A", "url": "https://a.com"}]
        tmp_fixture_store.save_golden_records("test_site", golden)

        loaded = tmp_fixture_store.load_golden_records("test_site")
        assert loaded == golden

    def test_fixture_age(self, tmp_fixture_store: FixtureStore) -> None:
        pages = [("page_001", "https://test.com/search", "<html></html>")]
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            tmp_fixture_store.capture_pages("test_site", pages)
        )

        age = tmp_fixture_store.fixture_age_days("test_site")
        assert age is not None
        assert age >= 0
        assert age < 1

    def test_is_stale_when_no_fixtures(self, tmp_fixture_store: FixtureStore) -> None:
        assert tmp_fixture_store.is_stale("nonexistent")

    def test_nonexistent_returns_none(self, tmp_fixture_store: FixtureStore) -> None:
        assert tmp_fixture_store.load_fixture_html("nonexistent") is None
        assert tmp_fixture_store.load_fingerprint("nonexistent") is None
        assert tmp_fixture_store.load_golden_records("nonexistent") is None
