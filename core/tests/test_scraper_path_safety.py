from __future__ import annotations

from pathlib import Path

import pytest

from core.scraper.path_safety import safe_join, validate_site_id


def test_validate_site_id_accepts_slug() -> None:
    assert validate_site_id("vision_gsi_woonsocket") == "vision_gsi_woonsocket"


@pytest.mark.parametrize("bad", ["../etc/passwd", "a/b", "", " space", "toolong_" * 10])
def test_validate_site_id_rejects_unsafe(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_site_id(bad)


def test_safe_join_stays_within_base() -> None:
    base = Path("/tmp/example-base")
    joined = safe_join(base, "indeed")
    assert str(joined).endswith("/tmp/example-base/indeed")


def test_safe_join_rejects_symlink_escape(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    escaped = base / "escaped"
    escaped.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        safe_join(base, "escaped")
