"""Fixture capture, storage, structural fingerprinting, and refresh.

Supports the hybrid four-layer test framework:

- **Capture**: Navigate key pages via Playwright, save HTML + screenshot +
  structural fingerprint + metadata.
- **Fingerprint**: Hash the DOM skeleton (tag tree + class attrs + form fields)
  ignoring text content, so structural changes are detected even when content
  varies.
- **Compare**: Quantify structural similarity between two fingerprints to
  detect site drift.
- **Golden records**: Human-verified expected extraction output saved
  alongside fixtures for regression testing.

Storage layout::

    <configured_fixtures_dir>/<site_id>/<capture_date>/
      page_001.html
      page_001.png
      page_001.meta.json
      fingerprint.json
      golden_records.json   (optional, human-curated)
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from core.config import get_settings
from core.scraper.path_safety import safe_join, validate_site_id

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = structlog.get_logger(__name__)

_DEFAULT_FIXTURES_DIR = Path(get_settings().scraper_fixtures_dir)

_TAG_RE = re.compile(r"<(\w+)(?:\s+([^>]*?))?\s*/?>", re.DOTALL)
_CLASS_RE = re.compile(r'class\s*=\s*"([^"]*)"')
_NAME_RE = re.compile(r'name\s*=\s*"([^"]*)"')
_ID_RE = re.compile(r'id\s*=\s*"([^"]*)"')
_TYPE_RE = re.compile(r'type\s*=\s*"([^"]*)"')


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------


@dataclass
class StructuralFingerprint:
    """Token-efficient representation of a page's DOM skeleton."""

    tag_tree_hash: str = ""
    form_fields: list[str] = field(default_factory=list)
    key_classes: list[str] = field(default_factory=list)
    key_ids: list[str] = field(default_factory=list)
    interactive_element_count: int = 0
    total_tags: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag_tree_hash": self.tag_tree_hash,
            "form_fields": self.form_fields,
            "key_classes": self.key_classes,
            "key_ids": self.key_ids,
            "interactive_element_count": self.interactive_element_count,
            "total_tags": self.total_tags,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StructuralFingerprint:
        return cls(
            tag_tree_hash=data.get("tag_tree_hash", ""),
            form_fields=data.get("form_fields", []),
            key_classes=data.get("key_classes", []),
            key_ids=data.get("key_ids", []),
            interactive_element_count=data.get("interactive_element_count", 0),
            total_tags=data.get("total_tags", 0),
        )


def compute_fingerprint(html: str) -> StructuralFingerprint:
    """Compute a structural fingerprint from raw HTML.

    Hashes the DOM tag tree (tag names + class attributes + form field names)
    while ignoring text content, so structural drift is detected even when
    the data on the page varies between visits.
    """
    skeleton_parts: list[str] = []
    form_fields: list[str] = []
    key_classes: set[str] = set()
    key_ids: set[str] = set()
    interactive_count = 0
    total_tags = 0

    for match in _TAG_RE.finditer(html):
        tag = match.group(1).lower()
        attrs = match.group(2) or ""
        total_tags += 1

        cls_match = _CLASS_RE.search(attrs)
        cls_str = cls_match.group(1) if cls_match else ""

        skeleton_parts.append(f"{tag}:{cls_str}")

        if cls_str:
            for c in cls_str.split():
                if len(c) > 2 and not c.startswith("_"):
                    key_classes.add(c)

        id_match = _ID_RE.search(attrs)
        if id_match and id_match.group(1):
            key_ids.add(id_match.group(1))

        if tag in ("input", "select", "textarea", "button"):
            interactive_count += 1
            name_match = _NAME_RE.search(attrs)
            type_match = _TYPE_RE.search(attrs)
            field_desc = f"{tag}"
            if name_match:
                field_desc += f"[name={name_match.group(1)}]"
            if type_match:
                field_desc += f"[type={type_match.group(1)}]"
            form_fields.append(field_desc)

    skeleton_str = "\n".join(skeleton_parts)
    tree_hash = hashlib.sha256(skeleton_str.encode()).hexdigest()

    return StructuralFingerprint(
        tag_tree_hash=tree_hash,
        form_fields=sorted(set(form_fields)),
        key_classes=sorted(key_classes)[:100],
        key_ids=sorted(key_ids)[:50],
        interactive_element_count=interactive_count,
        total_tags=total_tags,
    )


@dataclass
class DriftReport:
    """Result of comparing two structural fingerprints."""

    similarity: float
    tree_changed: bool
    added_classes: list[str] = field(default_factory=list)
    removed_classes: list[str] = field(default_factory=list)
    added_fields: list[str] = field(default_factory=list)
    removed_fields: list[str] = field(default_factory=list)
    severity: str = "none"

    @property
    def drifted(self) -> bool:
        return self.severity != "none"


def compare_fingerprints(
    old: StructuralFingerprint,
    new: StructuralFingerprint,
    *,
    threshold: float = 0.85,
) -> DriftReport:
    """Compare two fingerprints and produce a drift report.

    Similarity is computed as a weighted average of:
    - Tag tree hash match (40%)
    - Form field overlap (30%)
    - Key class overlap (20%)
    - Interactive element count proximity (10%)
    """
    tree_same = old.tag_tree_hash == new.tag_tree_hash
    tree_score = 1.0 if tree_same else 0.0

    old_fields = set(old.form_fields)
    new_fields = set(new.form_fields)
    if old_fields or new_fields:
        field_overlap = len(old_fields & new_fields) / max(len(old_fields | new_fields), 1)
    else:
        field_overlap = 1.0

    old_classes = set(old.key_classes)
    new_classes = set(new.key_classes)
    if old_classes or new_classes:
        class_overlap = len(old_classes & new_classes) / max(len(old_classes | new_classes), 1)
    else:
        class_overlap = 1.0

    if max(old.interactive_element_count, new.interactive_element_count) > 0:
        count_ratio = min(old.interactive_element_count, new.interactive_element_count) / max(
            old.interactive_element_count, new.interactive_element_count
        )
    else:
        count_ratio = 1.0

    similarity = (
        0.40 * tree_score + 0.30 * field_overlap + 0.20 * class_overlap + 0.10 * count_ratio
    )

    if similarity >= threshold:
        severity = "none"
    elif similarity >= 0.6:
        severity = "low"
    elif similarity >= 0.3:
        severity = "medium"
    else:
        severity = "high"

    return DriftReport(
        similarity=round(similarity, 4),
        tree_changed=not tree_same,
        added_classes=sorted(new_classes - old_classes),
        removed_classes=sorted(old_classes - new_classes),
        added_fields=sorted(new_fields - old_fields),
        removed_fields=sorted(old_fields - new_fields),
        severity=severity,
    )


# ---------------------------------------------------------------------------
# Fixture storage
# ---------------------------------------------------------------------------


class FixtureStore:
    """Manages capture, storage, and retrieval of site fixtures."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._base_dir = Path(base_dir or _DEFAULT_FIXTURES_DIR)

    def _site_dir(self, site_id: str) -> Path:
        return safe_join(self._base_dir, site_id)

    def _latest_capture_dir(self, site_id: str) -> Path | None:
        validate_site_id(site_id)
        site_dir = self._site_dir(site_id)
        if not site_dir.exists():
            return None
        captures = sorted(
            (d for d in site_dir.iterdir() if d.is_dir()),
            reverse=True,
        )
        return captures[0] if captures else None

    def _new_capture_dir(self, site_id: str) -> Path:
        validate_site_id(site_id)
        d = self._site_dir(site_id) / date.today().isoformat()
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -- capture -------------------------------------------------------------

    async def capture_page(
        self,
        site_id: str,
        page: Page,
        *,
        page_label: str = "page_001",
        capture_dir: Path | None = None,
    ) -> Path:
        """Capture HTML + screenshot + fingerprint + metadata for one page."""
        validate_site_id(site_id)
        cap_dir = capture_dir or self._new_capture_dir(site_id)

        html = await page.content()
        html_path = cap_dir / f"{page_label}.html"
        html_path.write_text(html, encoding="utf-8")

        screenshot_path = cap_dir / f"{page_label}.png"
        try:
            await page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception as exc:
            logger.warning("fixtures.screenshot_failed", site_id=site_id, error=str(exc)[:200])

        fp = compute_fingerprint(html)
        meta = {
            "url": page.url,
            "title": await page.title(),
            "captured_at": datetime.now(UTC).isoformat(),
            "page_label": page_label,
            "fingerprint": fp.to_dict(),
        }
        meta_path = cap_dir / f"{page_label}.meta.json"
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        logger.info(
            "fixtures.page_captured",
            site_id=site_id,
            label=page_label,
            path=str(cap_dir),
        )
        return cap_dir

    async def capture_pages(
        self,
        site_id: str,
        pages_html: list[tuple[str, str, str]],
    ) -> Path:
        """Capture multiple pages from raw HTML (no browser needed).

        Args:
            pages_html: List of (page_label, url, html) tuples.
        """
        validate_site_id(site_id)
        cap_dir = self._new_capture_dir(site_id)
        fingerprints: list[dict[str, Any]] = []

        for page_label, url, html in pages_html:
            html_path = cap_dir / f"{page_label}.html"
            html_path.write_text(html, encoding="utf-8")

            fp = compute_fingerprint(html)
            meta = {
                "url": url,
                "captured_at": datetime.now(UTC).isoformat(),
                "page_label": page_label,
                "fingerprint": fp.to_dict(),
            }
            meta_path = cap_dir / f"{page_label}.meta.json"
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            fingerprints.append(fp.to_dict())

        aggregate = self._aggregate_fingerprint(fingerprints)
        fp_path = cap_dir / "fingerprint.json"
        fp_path.write_text(json.dumps(aggregate, indent=2), encoding="utf-8")

        logger.info("fixtures.captured", site_id=site_id, pages=len(pages_html), path=str(cap_dir))
        return cap_dir

    def _aggregate_fingerprint(self, fingerprints: list[dict[str, Any]]) -> dict[str, Any]:
        all_classes: set[str] = set()
        all_fields: set[str] = set()
        all_ids: set[str] = set()
        hashes: list[str] = []
        total_tags = 0
        total_interactive = 0

        for fp in fingerprints:
            hashes.append(fp.get("tag_tree_hash", ""))
            all_classes.update(fp.get("key_classes", []))
            all_fields.update(fp.get("form_fields", []))
            all_ids.update(fp.get("key_ids", []))
            total_tags += fp.get("total_tags", 0)
            total_interactive += fp.get("interactive_element_count", 0)

        combined_hash = hashlib.sha256("|".join(hashes).encode()).hexdigest()
        return {
            "aggregate_hash": combined_hash,
            "page_count": len(fingerprints),
            "key_classes": sorted(all_classes)[:100],
            "form_fields": sorted(all_fields),
            "key_ids": sorted(all_ids)[:50],
            "total_tags": total_tags,
            "total_interactive": total_interactive,
            "captured_at": datetime.now(UTC).isoformat(),
        }

    # -- loading -------------------------------------------------------------

    def load_fixture_html(self, site_id: str, page_label: str = "page_001") -> str | None:
        """Load the latest fixture HTML for a given page label."""
        validate_site_id(site_id)
        cap_dir = self._latest_capture_dir(site_id)
        if cap_dir is None:
            return None
        html_path = cap_dir / f"{page_label}.html"
        if not html_path.is_file():
            return None
        return html_path.read_text(encoding="utf-8")

    def latest_fixture_path(self, site_id: str, page_label: str = "page_001") -> Path | None:
        """Return the latest fixture path for a site/page label."""
        validate_site_id(site_id)
        cap_dir = self._latest_capture_dir(site_id)
        if cap_dir is None:
            return None
        html_path = cap_dir / f"{page_label}.html"
        return html_path if html_path.is_file() else None

    def load_all_fixtures(self, site_id: str) -> list[tuple[str, str]]:
        """Load all fixture HTML files from the latest capture.

        Returns list of (page_label, html) tuples.
        """
        validate_site_id(site_id)
        cap_dir = self._latest_capture_dir(site_id)
        if cap_dir is None:
            return []
        results: list[tuple[str, str]] = []
        for path in sorted(cap_dir.glob("*.html")):
            label = path.stem
            html = path.read_text(encoding="utf-8")
            results.append((label, html))
        return results

    def load_fingerprint(self, site_id: str) -> StructuralFingerprint | None:
        """Load the aggregate fingerprint from the latest capture."""
        validate_site_id(site_id)
        cap_dir = self._latest_capture_dir(site_id)
        if cap_dir is None:
            return None
        fp_path = cap_dir / "fingerprint.json"
        if not fp_path.is_file():
            for meta_path in cap_dir.glob("*.meta.json"):
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    fp_data = meta.get("fingerprint")
                    if fp_data:
                        return StructuralFingerprint.from_dict(fp_data)
                except (json.JSONDecodeError, OSError):
                    continue
            return None
        try:
            data = json.loads(fp_path.read_text(encoding="utf-8"))
            return StructuralFingerprint(
                tag_tree_hash=data.get("aggregate_hash", ""),
                form_fields=data.get("form_fields", []),
                key_classes=data.get("key_classes", []),
                key_ids=data.get("key_ids", []),
                interactive_element_count=data.get("total_interactive", 0),
                total_tags=data.get("total_tags", 0),
            )
        except (json.JSONDecodeError, OSError):
            return None

    def load_golden_records(self, site_id: str) -> list[dict[str, Any]] | None:
        """Load golden records from the latest capture (if they exist)."""
        validate_site_id(site_id)
        cap_dir = self._latest_capture_dir(site_id)
        if cap_dir is None:
            return None
        golden_path = cap_dir / "golden_records.json"
        if not golden_path.is_file():
            return None
        try:
            return json.loads(golden_path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
        except (json.JSONDecodeError, OSError):
            return None

    def save_golden_records(self, site_id: str, records: list[dict[str, Any]]) -> Path:
        """Save human-verified golden records alongside the latest fixture."""
        validate_site_id(site_id)
        cap_dir = self._latest_capture_dir(site_id)
        if cap_dir is None:
            cap_dir = self._new_capture_dir(site_id)
        golden_path = cap_dir / "golden_records.json"
        golden_path.write_text(
            json.dumps(records, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        logger.info(
            "fixtures.golden_saved", site_id=site_id, records=len(records), path=str(golden_path)
        )
        return golden_path

    # -- age / freshness -----------------------------------------------------

    def fixture_age_days(self, site_id: str) -> float | None:
        """Days since the latest capture. None if no fixtures exist."""
        validate_site_id(site_id)
        cap_dir = self._latest_capture_dir(site_id)
        if cap_dir is None:
            return None
        try:
            capture_date = date.fromisoformat(cap_dir.name)
            return (date.today() - capture_date).days
        except ValueError:
            return None

    def is_stale(self, site_id: str, max_age_days: int = 7) -> bool:
        """Check if fixtures need refreshing."""
        validate_site_id(site_id)
        age = self.fixture_age_days(site_id)
        if age is None:
            return True
        return age > max_age_days
