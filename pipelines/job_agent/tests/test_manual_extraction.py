"""Tests for direct URL job extraction and manual extraction node."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from pipelines.job_agent.extraction.manual_job_extractor import (
    ExtractedJobData,
    canonicalize_job_url,
    detect_provider,
    extract_job_data_from_html,
    generate_dedup_key,
    map_provider_to_source,
)
from pipelines.job_agent.graph import build_manual_graph
from pipelines.job_agent.models import JobSource, SearchCriteria
from pipelines.job_agent.nodes.manual_extraction import manual_extraction_node
from pipelines.job_agent.state import JobAgentState, PipelinePhase

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


class TestExtractionHelpers:
    def test_provider_detection(self) -> None:
        assert detect_provider("https://www.linkedin.com/jobs/view/123") == "linkedin"
        assert detect_provider("https://boards.greenhouse.io/acme/jobs/123") == "greenhouse"
        assert detect_provider("https://jobs.ashbyhq.com/acme/123") == "ashby"

    def test_source_mapping(self) -> None:
        assert map_provider_to_source("linkedin") == JobSource.LINKEDIN
        assert map_provider_to_source("greenhouse") == JobSource.GREENHOUSE
        assert map_provider_to_source("ashby") == JobSource.OTHER

    def test_canonicalize_url(self) -> None:
        url = "https://example.com/jobs/123?utm_source=x&gh_jid=999#section"
        assert canonicalize_job_url(url) == "https://example.com/jobs/123?gh_jid=999"

    def test_generate_dedup_key_deterministic(self) -> None:
        key1 = generate_dedup_key("Acme", "TPM", "https://example.com/job")
        key2 = generate_dedup_key("acme", "tpm", "https://example.com/job")
        assert key1 == key2


class TestExtractionFromHtml:
    def test_jsonld_extraction(self) -> None:
        data = extract_job_data_from_html(
            "https://acme.example/jobs/123",
            _fixture("job_page_jsonld.html"),
        )
        assert data.title == "Senior Program Manager"
        assert data.company == "Acme Robotics"
        assert data.location == "Boston, MA"
        assert data.salary_min == 190000
        assert data.salary_max == 230000
        assert data.remote is True
        assert "cross-functional strategic initiatives" in data.normalized_description.lower()

    def test_provider_specific_extraction(self) -> None:
        data = extract_job_data_from_html(
            "https://www.linkedin.com/jobs/view/4383883967/",
            _fixture("job_page_linkedin_like.html"),
        )
        assert data.source == JobSource.LINKEDIN
        assert data.company == "Boston Dynamics"
        assert data.location == "Waltham, MA"
        assert "responsibilities" in data.normalized_description.lower()

    def test_generic_extraction_and_salary_inference(self) -> None:
        data = extract_job_data_from_html(
            "https://careers.example.com/openings/program-director",
            _fixture("job_page_generic.html"),
        )
        assert data.source == JobSource.COMPANY_SITE
        assert data.title == "Program Director"
        assert data.salary_min == 210000
        assert data.salary_max == 260000
        assert data.metadata["remote_mode"] == "hybrid"
        assert data.remote is None

    def test_sparse_page_fallback(self) -> None:
        data = extract_job_data_from_html(
            "https://careers.example.com/jobs/ops-manager",
            _fixture("job_page_sparse.html"),
        )
        assert data.normalized_description
        assert "pmo reporting" in data.normalized_description.lower()
        assert data.remote is True
        assert data.employment_type == "full-time"


class TestManualExtractionNode:
    @pytest.mark.asyncio
    async def test_manual_node_populates_listing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _stub_extract(_: str) -> ExtractedJobData:
            return ExtractedJobData(
                title="Head of Programs",
                company="North Harbor",
                location="Boston, MA",
                canonical_url="https://example.com/jobs/abc",
                source=JobSource.OTHER,
                raw_description="Raw text",
                normalized_description="Normalized text",
                salary_min=200000,
                salary_max=240000,
                remote=False,
                employment_type="full-time",
                role_summary="Lead strategic programs.",
                metadata={"provider": "company_site", "remote_mode": "onsite"},
            )

        monkeypatch.setattr(
            "pipelines.job_agent.nodes.manual_extraction.extract_job_data_from_url",
            _stub_extract,
        )

        state = JobAgentState(
            search_criteria=SearchCriteria(),
            manual_job_url="https://example.com/jobs/abc",
            run_id="manual-test",
        )
        out = await manual_extraction_node(state)
        assert out.phase == PipelinePhase.DISCOVERY
        assert len(out.discovered_listings) == 1
        assert len(out.qualified_listings) == 1
        assert out.qualified_listings[0].description == "Normalized text"
        assert out.errors == []

    @pytest.mark.asyncio
    async def test_manual_node_requires_url(self) -> None:
        state = JobAgentState(search_criteria=SearchCriteria(), run_id="manual-test")
        out = await manual_extraction_node(state)
        assert out.qualified_listings == []
        assert out.errors
        assert out.errors[0]["node"] == "manual_extraction"

    @pytest.mark.asyncio
    async def test_manual_graph_missing_url_routes_to_notification(self) -> None:
        graph = build_manual_graph()
        initial = JobAgentState(search_criteria=SearchCriteria(), run_id="manual-graph")
        final = await graph.ainvoke(initial)
        if isinstance(final, dict):
            assert final["phase"] == PipelinePhase.COMPLETE
            assert final["qualified_listings"] == []
            return
        typed = cast("JobAgentState", final)
        assert typed.phase == PipelinePhase.COMPLETE
        assert typed.qualified_listings == []
