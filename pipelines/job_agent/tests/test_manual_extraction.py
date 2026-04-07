"""Tests for direct URL job extraction and manual extraction node."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from bs4 import BeautifulSoup

from pipelines.job_agent.extraction.inspection import (
    write_extracted_job_markdown,
    write_job_analysis_markdown,
)
from pipelines.job_agent.extraction.manual_job_extractor import (
    ExtractedJobData,
    _clone_without_noise,
    canonicalize_job_url,
    detect_provider,
    extract_job_data_from_html,
    generate_dedup_key,
    map_provider_to_source,
)
from pipelines.job_agent.graph import build_manual_graph
from pipelines.job_agent.models import JobListing, JobSource, SearchCriteria
from pipelines.job_agent.models.resume_tailoring import JobAnalysisResult
from pipelines.job_agent.nodes.manual_extraction import manual_extraction_node
from pipelines.job_agent.state import JobAgentState, PipelinePhase

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


class TestCloneWithoutNoise:
    def test_tolerates_tag_with_none_attrs(self) -> None:
        """Real pages (e.g. LinkedIn) can produce malformed tags where ``attrs`` is None."""
        soup = BeautifulSoup("<html><body><div>keep</div></body></html>", "html.parser")
        div = soup.find("div")
        assert div is not None
        div.attrs = None  # type: ignore[assignment]
        out = _clone_without_noise(soup)
        assert "keep" in out.get_text()


class TestExtractionHelpers:
    def test_provider_detection(self) -> None:
        assert detect_provider("https://www.linkedin.com/jobs/view/123") == "linkedin"
        assert detect_provider("https://boards.greenhouse.io/acme/jobs/123") == "greenhouse"
        assert detect_provider("https://jobs.ashbyhq.com/acme/123") == "ashby"
        assert detect_provider("https://www.amazon.jobs/en/jobs/3185564/foo") == "amazon"

    def test_source_mapping(self) -> None:
        assert map_provider_to_source("linkedin") == JobSource.LINKEDIN
        assert map_provider_to_source("indeed") == JobSource.INDEED
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
    def test_raw_and_cleaned_description_fields(self) -> None:
        html = """
        <html><body>
          <h1>Role</h1>
          <main>
            <h2>Responsibilities</h2>
            <p>Lead delivery.</p>
            <p>Lead delivery.</p>
          </main>
        </body></html>
        """
        data = extract_job_data_from_html("https://careers.example.com/jobs/role", html)
        assert data.raw_description
        assert data.cleaned_description
        assert data.normalized_description == data.cleaned_description
        assert len(data.cleaned_description) <= len(data.raw_description)

    def test_prefers_richer_visible_block_over_sparse_structured(self) -> None:
        html = """
        <html><head>
          <script type="application/ld+json">
            {"@type":"JobPosting","title":"Program Manager","description":"Great role."}
          </script>
        </head><body>
          <h1>Program Manager</h1>
          <div class="job-description">
            <h2>Responsibilities</h2>
            <ul><li>Lead planning</li><li>Own roadmap</li></ul>
            <h2>Basic Qualifications</h2>
            <ul><li>7+ years experience</li></ul>
            <h2>Preferred Qualifications</h2>
            <ul><li>MBA preferred</li></ul>
          </div>
        </body></html>
        """
        data = extract_job_data_from_html("https://careers.example.com/jobs/pm", html)
        desc = data.cleaned_description.lower()
        assert "basic qualifications" in desc
        assert "preferred qualifications" in desc
        assert data.metadata["extraction_mode"] in {"provider", "generic"}

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
        assert data.company == "Example Company"

    def test_generic_company_extraction_from_title_pattern(self) -> None:
        html = """
        <html><head><title>Program Director - Orbital Systems</title></head>
        <body>
          <h1>Program Director - Orbital Systems</h1>
          <main>
            <h2>Responsibilities</h2>
            <p>Own cross-functional delivery and hiring plans.</p>
          </main>
        </body></html>
        """
        data = extract_job_data_from_html("https://careers.orbital.example/jobs/1", html)
        assert data.company == "Orbital Systems"

    def test_amazon_like_captures_qualifications(self) -> None:
        data = extract_job_data_from_html(
            "https://www.amazon.jobs/en/jobs/3185564/principal-product-manager",
            _fixture("job_page_amazon_like.html"),
        )
        assert data.source == JobSource.COMPANY_SITE
        desc = data.normalized_description.lower()
        assert "basic qualifications" in desc
        assert "preferred qualifications" in desc
        assert "10+ years of end to end product delivery" in desc
        assert "lead product definition" in desc
        assert data.salary_min is not None
        assert data.salary_max is not None

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
                cleaned_description="Normalized text",
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
        assert out.qualified_listings[0].notes is not None
        assert "raw_description" in out.qualified_listings[0].notes
        assert out.errors == []

    @pytest.mark.asyncio
    async def test_manual_node_dry_run_skips_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _boom(_: str) -> ExtractedJobData:
            raise AssertionError("extract should not run in dry_run")

        monkeypatch.setattr(
            "pipelines.job_agent.nodes.manual_extraction.extract_job_data_from_url",
            _boom,
        )
        state = JobAgentState(
            search_criteria=SearchCriteria(),
            manual_job_url="https://example.com/jobs/abc",
            run_id="manual-test",
            dry_run=True,
        )
        out = await manual_extraction_node(state)
        assert out.qualified_listings == []
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


class TestExtractedJobMarkdown:
    """Inspection outputs: full scraped description + structured analysis."""

    def test_extracted_markdown_shows_full_description(self, tmp_path: Path) -> None:
        listing = JobListing(
            title="Engineer",
            company="Acme Corp",
            location="Boston, MA",
            url="https://example.com/jobs/1",
            source=JobSource.LINKEDIN,
            description="Responsibilities line one.\nRequirements line two.",
            dedup_key="a" * 32,
        )
        out = write_extracted_job_markdown(listing, run_id="run1", output_root=tmp_path)
        assert out.parent == tmp_path / "run1"
        text = out.read_text(encoding="utf-8")
        assert "**Title:** Engineer" in text
        assert "**Company:** Acme Corp" in text
        assert "Responsibilities line one" in text
        assert "Requirements line two" in text
        assert "untruncated" in text.lower()

    def test_analysis_markdown_shows_structured_output(self, tmp_path: Path) -> None:
        listing = JobListing(
            title="TPM",
            company="Defense Co",
            url="https://example.com/jobs/2",
            source=JobSource.COMPANY_SITE,
            description="Full job description here.",
            dedup_key="b" * 32,
        )
        analysis = JobAnalysisResult(
            themes=["autonomous systems"],
            seniority="senior",
            domain_tags=["defense"],
            must_hit_keywords=["autonomous"],
            priority_requirements=["5+ years"],
            basic_qualifications=["BS in CS"],
            preferred_qualifications=["Clearance"],
            angles=["defense to product"],
        )
        out = write_job_analysis_markdown(listing, analysis, run_id="run1", output_root=tmp_path)
        text = out.read_text(encoding="utf-8")
        assert "autonomous systems" in text
        assert "BS in CS" in text
        assert "Clearance" in text
        assert "senior" in text
        assert "defense to product" in text


class TestExtractionFromUrlFlow:
    @pytest.mark.asyncio
    async def test_provider_detection_uses_resolved_final_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.fetch.types import FetchMethod, FetchResult
        from pipelines.job_agent.extraction.manual_job_extractor import extract_job_data_from_url

        class _HttpStub:
            async def fetch(self, url: str) -> FetchResult:
                return FetchResult(
                    html=_fixture("job_page_generic.html"),
                    final_url="https://boards.greenhouse.io/acme/jobs/123",
                    status_code=200,
                    method=FetchMethod.HTTP,
                )

        monkeypatch.setattr(
            "pipelines.job_agent.extraction.manual_job_extractor.HttpFetcher",
            _HttpStub,
        )
        data = await extract_job_data_from_url("https://acme.com/careers/job-123")
        assert data.source == JobSource.GREENHOUSE
        assert data.metadata["resolved_provider"] == "greenhouse"

    @pytest.mark.asyncio
    async def test_browser_fallback_prefers_higher_quality_not_length(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.fetch.types import FetchMethod, FetchResult
        from pipelines.job_agent.extraction.manual_job_extractor import extract_job_data_from_url

        low_quality_long = "<html><body>" + ("apply now " * 800) + "</body></html>"
        high_quality_shorter = """
        <html><body><h1>Program Manager</h1>
        <div><h2>Responsibilities</h2><ul><li>Lead planning</li></ul>
        <h2>Basic Qualifications</h2><ul><li>7+ years</li></ul>
        <h2>Preferred Qualifications</h2><ul><li>MBA</li></ul></div>
        </body></html>
        """

        class _HttpStub:
            async def fetch(self, url: str) -> FetchResult:
                return FetchResult(
                    html=low_quality_long,
                    final_url="https://careers.example.com/jobs/1",
                    status_code=200,
                    method=FetchMethod.HTTP,
                )

        class _BrowserStub:
            async def fetch(self, url: str) -> FetchResult:
                return FetchResult(
                    html=high_quality_shorter,
                    final_url="https://careers.example.com/jobs/1",
                    status_code=200,
                    method=FetchMethod.BROWSER,
                )

        monkeypatch.setattr(
            "pipelines.job_agent.extraction.manual_job_extractor.HttpFetcher",
            _HttpStub,
        )
        monkeypatch.setattr(
            "pipelines.job_agent.extraction.manual_job_extractor.BrowserFetcher",
            _BrowserStub,
        )

        data = await extract_job_data_from_url("https://careers.example.com/jobs/1")
        assert data.metadata["extraction_winner"] == "browser"
        assert "basic qualifications" in data.cleaned_description.lower()


    @pytest.mark.asyncio
    async def test_browser_fallback_subtle_quality_difference(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both HTTP and browser have real content; browser wins on section richness."""
        from core.fetch.types import FetchMethod, FetchResult
        from pipelines.job_agent.extraction.manual_job_extractor import extract_job_data_from_url

        http_html = """
        <html><body><h1>Senior Engineer</h1>
        <div>
          <h2>About the role</h2>
          <p>Join our team to build amazing distributed systems at scale.</p>
          <p>You will work across the full stack on real-time data pipelines.</p>
          <p>We are looking for engineers with strong fundamentals.</p>
          <p>Competitive salary and benefits. Remote friendly.</p>
          <p>Full-time position starting immediately.</p>
          <p>Experience with Python and cloud infrastructure preferred.</p>
        </div>
        </body></html>
        """
        browser_html = """
        <html><body><h1>Senior Engineer</h1>
        <span class="company">Acme Platforms</span>
        <div>
          <h2>Responsibilities</h2>
          <ul>
            <li>Design and build distributed systems at scale</li>
            <li>Own full lifecycle of real-time data pipelines</li>
          </ul>
          <h2>Basic Qualifications</h2>
          <ul>
            <li>5+ years software engineering experience</li>
            <li>Strong Python and cloud infrastructure skills</li>
          </ul>
          <h2>Preferred Qualifications</h2>
          <ul>
            <li>Experience with Kafka, Spark, or similar frameworks</li>
            <li>MS in Computer Science</li>
          </ul>
        </div>
        </body></html>
        """

        class _HttpStub:
            async def fetch(self, url: str) -> FetchResult:
                return FetchResult(
                    html=http_html,
                    final_url="https://careers.acme.example/jobs/123",
                    status_code=200,
                    method=FetchMethod.HTTP,
                )

        class _BrowserStub:
            async def fetch(self, url: str) -> FetchResult:
                return FetchResult(
                    html=browser_html,
                    final_url="https://careers.acme.example/jobs/123",
                    status_code=200,
                    method=FetchMethod.BROWSER,
                )

        monkeypatch.setattr(
            "pipelines.job_agent.extraction.manual_job_extractor.HttpFetcher",
            _HttpStub,
        )
        monkeypatch.setattr(
            "pipelines.job_agent.extraction.manual_job_extractor.BrowserFetcher",
            _BrowserStub,
        )

        data = await extract_job_data_from_url("https://careers.acme.example/jobs/123")
        assert data.metadata["browser_fallback_used"] is True
        assert data.metadata["extraction_winner"] == "browser"
        assert "basic qualifications" in data.cleaned_description.lower()
        assert "preferred qualifications" in data.cleaned_description.lower()


class TestManualRunId:
    def test_default_manual_run_id_is_unique_and_prefixed(self) -> None:
        from scripts.run_manual_url_tailor import _default_run_id

        run_id_1 = _default_run_id("https://example.com/jobs/1")
        run_id_2 = _default_run_id("https://example.com/jobs/1")
        assert run_id_1.startswith("manual-url-")
        assert run_id_2.startswith("manual-url-")
        assert run_id_1 != run_id_2
