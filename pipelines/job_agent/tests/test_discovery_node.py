"""Tests for discovery_node and bulk_extraction_node."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from pipelines.job_agent.discovery.models import ListingRef, ProviderResult
from pipelines.job_agent.models import ApplicationStatus, JobListing, JobSource, SearchCriteria
from pipelines.job_agent.nodes.bulk_extraction import bulk_extraction_node
from pipelines.job_agent.nodes.discovery import discovery_node
from pipelines.job_agent.state import JobAgentState, PipelinePhase


def _make_ref(
    url: str = "https://example.com/job/1",
    title: str = "Software Engineer",
    company: str = "Acme",
) -> ListingRef:
    return ListingRef(url=url, title=title, company=company, source=JobSource.LINKEDIN)


def _make_listing(
    url: str = "https://example.com/job/1",
    title: str = "Software Engineer",
    company: str = "Acme",
    description: str = "",
) -> JobListing:
    return JobListing(
        title=title,
        company=company,
        url=url,
        source=JobSource.LINKEDIN,
        description=description,
        status=ApplicationStatus.DISCOVERED,
        dedup_key="abc123def456",
    )


# ---------------------------------------------------------------------------
# Discovery node
# ---------------------------------------------------------------------------


class TestDiscoveryNode:
    @pytest.mark.asyncio
    async def test_dry_run_skips(self) -> None:
        state = JobAgentState(dry_run=True)
        result = await discovery_node(state)
        assert result.phase == PipelinePhase.DISCOVERY
        assert result.discovered_listings == []

    @pytest.mark.asyncio
    async def test_orchestrator_results_become_listings(self) -> None:
        refs = [_make_ref(), _make_ref(url="https://example.com/job/2", title="PM")]

        with (
            patch("pipelines.job_agent.nodes.discovery.get_settings") as mock_settings,
            patch("pipelines.job_agent.nodes.discovery.DiscoveryOrchestrator") as mock_orch_cls,
            patch(
                "pipelines.job_agent.nodes.discovery.deduplicate_refs",
                new_callable=AsyncMock,
                return_value=refs,
            ),
            patch(
                "pipelines.job_agent.nodes.discovery.apply_prefilter",
                return_value=(refs, []),
            ),
        ):
            mock_settings.return_value = MagicMock(
                discovery_sessions_dir="/tmp/s",
                discovery_max_concurrent_providers=1,
                discovery_max_pages_per_search=2,
                discovery_max_listings_per_provider=50,
                discovery_session_max_age_hours=72,
                discovery_prefilter_min_score=0.0,
                discovery_debug_capture_enabled=False,
                discovery_debug_capture_dir="data/debug_captures",
                discovery_debug_capture_html=True,
                discovery_linkedin_enabled=False,
                discovery_indeed_enabled=False,
                discovery_builtin_enabled=False,
                discovery_wellfound_enabled=False,
                discovery_greenhouse_enabled=False,
                discovery_lever_enabled=False,
                discovery_workday_enabled=False,
                greenhouse_company_list=[],
                lever_company_list=[],
                workday_company_list=[],
                direct_site_configs="",
                captcha_strategy="avoid",
                captcha_api_key=SecretStr(""),
            )
            mock_orch = mock_orch_cls.return_value
            mock_orch.run = AsyncMock(return_value=refs)

            state = JobAgentState(search_criteria=SearchCriteria(keywords=["engineer"]))
            result = await discovery_node(state)

        assert len(result.discovered_listings) == 2
        assert result.discovered_listings[0].title == "Software Engineer"
        assert result.discovered_listings[1].title == "PM"

    @pytest.mark.asyncio
    async def test_prefilter_rejects_low_score(self) -> None:
        high = _make_ref(title="Staff Engineer")
        low = _make_ref(url="https://example.com/2", title="Nurse")

        with (
            patch("pipelines.job_agent.nodes.discovery.get_settings") as mock_settings,
            patch("pipelines.job_agent.nodes.discovery.DiscoveryOrchestrator") as mock_orch_cls,
            patch(
                "pipelines.job_agent.nodes.discovery.deduplicate_refs",
                new_callable=AsyncMock,
                return_value=[high, low],
            ),
            patch(
                "pipelines.job_agent.nodes.discovery.apply_prefilter",
                return_value=([high], [low]),
            ),
        ):
            mock_settings.return_value = MagicMock(
                discovery_sessions_dir="/tmp/s",
                discovery_max_concurrent_providers=1,
                discovery_max_pages_per_search=2,
                discovery_max_listings_per_provider=50,
                discovery_session_max_age_hours=72,
                discovery_prefilter_min_score=0.3,
                discovery_debug_capture_enabled=False,
                discovery_debug_capture_dir="data/debug_captures",
                discovery_debug_capture_html=True,
                discovery_linkedin_enabled=False,
                discovery_indeed_enabled=False,
                discovery_builtin_enabled=False,
                discovery_wellfound_enabled=False,
                discovery_greenhouse_enabled=False,
                discovery_lever_enabled=False,
                discovery_workday_enabled=False,
                greenhouse_company_list=[],
                lever_company_list=[],
                workday_company_list=[],
                direct_site_configs="",
                captcha_strategy="avoid",
                captcha_api_key=SecretStr(""),
            )
            mock_orch_cls.return_value.run = AsyncMock(return_value=[high, low])

            state = JobAgentState(search_criteria=SearchCriteria())
            result = await discovery_node(state)

        assert len(result.discovered_listings) == 1
        assert result.discovered_listings[0].title == "Staff Engineer"

    @pytest.mark.asyncio
    async def test_provider_errors_are_added_to_state_errors(self) -> None:
        ref = _make_ref()

        with (
            patch("pipelines.job_agent.nodes.discovery.get_settings") as mock_settings,
            patch("pipelines.job_agent.nodes.discovery.DiscoveryOrchestrator") as mock_orch_cls,
            patch(
                "pipelines.job_agent.nodes.discovery.deduplicate_refs",
                new_callable=AsyncMock,
                return_value=[ref],
            ),
            patch(
                "pipelines.job_agent.nodes.discovery.apply_prefilter",
                return_value=([ref], []),
            ),
        ):
            mock_settings.return_value = MagicMock(
                discovery_sessions_dir="/tmp/s",
                discovery_max_concurrent_providers=1,
                discovery_max_pages_per_search=2,
                discovery_max_listings_per_provider=50,
                discovery_session_max_age_hours=72,
                discovery_prefilter_min_score=0.0,
                discovery_debug_capture_enabled=False,
                discovery_debug_capture_dir="data/debug_captures",
                discovery_debug_capture_html=True,
                discovery_linkedin_enabled=True,
                discovery_indeed_enabled=False,
                discovery_builtin_enabled=False,
                discovery_wellfound_enabled=False,
                discovery_greenhouse_enabled=False,
                discovery_lever_enabled=False,
                discovery_workday_enabled=False,
                greenhouse_company_list=[],
                lever_company_list=[],
                workday_company_list=[],
                direct_site_configs="",
                captcha_strategy="avoid",
                captcha_api_key=SecretStr(""),
            )
            mock_orch = mock_orch_cls.return_value
            mock_orch.run = AsyncMock(return_value=[ref])
            mock_orch.last_provider_results = [
                ProviderResult(
                    source=JobSource.LINKEDIN,
                    refs=[],
                    errors=["auth_failed", "capture:/tmp/debug/metadata.json"],
                    pages_scraped=0,
                    session_saved=False,
                )
            ]

            state = JobAgentState(search_criteria=SearchCriteria(keywords=["engineer"]))
            result = await discovery_node(state)

        assert len(result.discovered_listings) == 1
        assert len(result.errors) == 2
        assert result.errors[0]["provider"] == JobSource.LINKEDIN.value
        assert "auth_failed" in result.errors[0]["message"]


# ---------------------------------------------------------------------------
# Bulk extraction node
# ---------------------------------------------------------------------------


class TestBulkExtractionNode:
    @pytest.mark.asyncio
    async def test_dry_run_skips(self) -> None:
        state = JobAgentState(dry_run=True, qualified_listings=[_make_listing()])
        result = await bulk_extraction_node(state)
        assert result.phase == PipelinePhase.BULK_EXTRACTION
        assert result.qualified_listings[0].description == ""

    @pytest.mark.asyncio
    async def test_empty_listings_skips(self) -> None:
        state = JobAgentState(qualified_listings=[])
        result = await bulk_extraction_node(state)
        assert result.phase == PipelinePhase.BULK_EXTRACTION

    @pytest.mark.asyncio
    async def test_successful_extraction(self) -> None:
        listing = _make_listing()
        extracted = MagicMock()
        extracted.cleaned_description = "Full job description here."
        extracted.title = "Senior SWE"
        extracted.company = "Acme Corp"
        extracted.location = "San Francisco"
        extracted.salary_min = 200_000
        extracted.salary_max = 300_000
        extracted.remote = True

        with (
            patch(
                "pipelines.job_agent.nodes.bulk_extraction.extract_job_data_from_url",
                new_callable=AsyncMock,
                return_value=extracted,
            ),
            patch(
                "pipelines.job_agent.nodes.bulk_extraction.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            state = JobAgentState(qualified_listings=[listing])
            result = await bulk_extraction_node(state)

        assert result.qualified_listings[0].description == "Full job description here."
        assert result.qualified_listings[0].status == ApplicationStatus.DISCOVERED
        assert result.qualified_listings[0].remote is True
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_extraction_failure_sets_errored(self) -> None:
        listing = _make_listing()

        with (
            patch(
                "pipelines.job_agent.nodes.bulk_extraction.extract_job_data_from_url",
                new_callable=AsyncMock,
                side_effect=RuntimeError("network failure"),
            ),
            patch(
                "pipelines.job_agent.nodes.bulk_extraction.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            state = JobAgentState(qualified_listings=[listing])
            result = await bulk_extraction_node(state)

        assert result.qualified_listings[0].status == ApplicationStatus.ERRORED
        assert len(result.errors) == 1
        assert result.errors[0]["node"] == "bulk_extraction"

    @pytest.mark.asyncio
    async def test_partial_failure_continues(self) -> None:
        listing1 = _make_listing(url="https://example.com/1")
        listing1.dedup_key = "key1"
        listing2 = _make_listing(url="https://example.com/2")
        listing2.dedup_key = "key2"

        extracted = MagicMock()
        extracted.cleaned_description = "Description."
        extracted.title = "SWE"
        extracted.company = "Co"
        extracted.location = ""
        extracted.salary_min = None
        extracted.salary_max = None
        extracted.remote = None

        call_count = 0

        async def _side_effect(url: str) -> object:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("fail")
            return extracted

        with (
            patch(
                "pipelines.job_agent.nodes.bulk_extraction.extract_job_data_from_url",
                new_callable=AsyncMock,
                side_effect=_side_effect,
            ),
            patch(
                "pipelines.job_agent.nodes.bulk_extraction.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            state = JobAgentState(qualified_listings=[listing1, listing2])
            result = await bulk_extraction_node(state)

        assert result.qualified_listings[0].status == ApplicationStatus.ERRORED
        assert result.qualified_listings[1].status == ApplicationStatus.DISCOVERED
        assert result.qualified_listings[1].description == "Description."
        assert len(result.errors) == 1

    @pytest.mark.asyncio
    async def test_already_populated_skipped(self) -> None:
        listing = _make_listing(description="Already has content.")

        with patch(
            "pipelines.job_agent.nodes.bulk_extraction.extract_job_data_from_url",
            new_callable=AsyncMock,
        ) as mock_extract:
            state = JobAgentState(qualified_listings=[listing])
            result = await bulk_extraction_node(state)

        mock_extract.assert_not_awaited()
        assert result.qualified_listings[0].description == "Already has content."
