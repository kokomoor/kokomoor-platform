from __future__ import annotations

from pipelines.scraper.nodes.scrape import resolve_wrapper
from pipelines.scraper.wrappers.base import BaseSiteWrapper
from pipelines.scraper.wrappers.linkedin import LinkedInWrapper
from pipelines.scraper.wrappers.vision_gsi import VisionGSIWrapper


def test_resolve_wrapper_exact_match() -> None:
    assert resolve_wrapper("linkedin") is LinkedInWrapper


def test_resolve_wrapper_prefix_match() -> None:
    assert resolve_wrapper("vision_gsi_woonsocket") is VisionGSIWrapper


def test_resolve_wrapper_fallback() -> None:
    assert resolve_wrapper("unknown_site") is BaseSiteWrapper
