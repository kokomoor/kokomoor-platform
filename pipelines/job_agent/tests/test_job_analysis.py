"""Tests for the job analysis node (structured JD extraction via LLM)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.testing import MockLLMClient
from pipelines.job_agent.models import ApplicationStatus, JobListing, JobSource, SearchCriteria
from pipelines.job_agent.state import JobAgentState


def _make_listing(
    *,
    description: str = "Build autonomous systems for defense applications.",
    dedup_key: str = "test_analysis_001",
) -> JobListing:
    return JobListing(
        title="Senior TPM",
        company="Acme Defense",
        location="Arlington, VA",
        url="https://example.com/jobs/tpm",
        source=JobSource.COMPANY_SITE,
        description=description,
        dedup_key=dedup_key,
    )


def _mock_analysis_json() -> str:
    return json.dumps(
        {
            "themes": ["autonomous systems", "defense technology"],
            "seniority": "senior",
            "domain_tags": ["defense", "tech"],
            "must_hit_keywords": ["autonomous", "defense", "product management"],
            "priority_requirements": ["5+ years engineering"],
            "basic_qualifications": ["BS in CS or equivalent", "5+ years experience"],
            "preferred_qualifications": ["MS preferred", "Clearance"],
            "angles": ["defense engineering to product"],
        }
    )


def _patch_settings(tmp_path: Path, *, model: str = "claude-haiku-4-5-20251001") -> None:
    import os

    from core.config import get_settings

    get_settings.cache_clear()
    os.environ["KP_RESUME_MASTER_PROFILE_PATH"] = str(
        Path(__file__).parent / "fixtures" / "master_profile.yaml"
    )
    os.environ["KP_RESUME_OUTPUT_DIR"] = str(tmp_path / "output")
    os.environ["KP_JOB_ANALYSIS_MODEL"] = model
    os.environ["KP_JOB_ANALYSIS_MAX_TOKENS"] = "2048"
    os.environ["KP_JOB_ANALYSIS_ENABLE_CACHE"] = "true"
    get_settings.cache_clear()


class TestJobAnalysisNode:
    @pytest.mark.asyncio
    async def test_analyses_qualified_listings(self, tmp_path: Path) -> None:
        from pipelines.job_agent.nodes.job_analysis import job_analysis_node

        mock_client = MockLLMClient(responses=[_mock_analysis_json()])
        state = JobAgentState(
            search_criteria=SearchCriteria(),
            qualified_listings=[_make_listing()],
            run_id="test-analysis",
        )
        _patch_settings(tmp_path)
        result = await job_analysis_node(state, llm_client=mock_client)

        assert "test_analysis_001" in result.job_analyses
        analysis = result.job_analyses["test_analysis_001"]
        assert analysis.seniority == "senior"
        assert "defense" in analysis.domain_tags
        assert len(analysis.basic_qualifications) == 2
        assert len(analysis.preferred_qualifications) == 2

    @pytest.mark.asyncio
    async def test_skips_dry_run(self) -> None:
        from pipelines.job_agent.nodes.job_analysis import job_analysis_node

        state = JobAgentState(
            search_criteria=SearchCriteria(),
            qualified_listings=[_make_listing()],
            run_id="test-dry",
            dry_run=True,
        )
        result = await job_analysis_node(state)
        assert len(result.job_analyses) == 0

    @pytest.mark.asyncio
    async def test_skips_empty_listings(self) -> None:
        from pipelines.job_agent.nodes.job_analysis import job_analysis_node

        state = JobAgentState(
            search_criteria=SearchCriteria(),
            qualified_listings=[],
            run_id="test-empty",
        )
        result = await job_analysis_node(state)
        assert len(result.job_analyses) == 0

    @pytest.mark.asyncio
    async def test_handles_empty_description(self, tmp_path: Path) -> None:
        from pipelines.job_agent.nodes.job_analysis import job_analysis_node

        mock_client = MockLLMClient(responses=["{}"])
        state = JobAgentState(
            search_criteria=SearchCriteria(),
            qualified_listings=[_make_listing(description="")],
            run_id="test-empty-desc",
        )
        _patch_settings(tmp_path)
        result = await job_analysis_node(state, llm_client=mock_client)

        assert len(result.errors) == 1
        assert "empty description" in result.errors[0]["message"].lower()
        assert "test_analysis_001" not in result.job_analyses
        assert result.qualified_listings[0].status == ApplicationStatus.ERRORED

    @pytest.mark.asyncio
    async def test_cache_reuses_result(self, tmp_path: Path) -> None:
        from pipelines.job_agent.nodes.job_analysis import job_analysis_node

        mock_client = MockLLMClient(responses=[_mock_analysis_json()])
        listing_a = _make_listing(dedup_key="same_key")
        listing_b = _make_listing(dedup_key="same_key")

        state = JobAgentState(
            search_criteria=SearchCriteria(),
            qualified_listings=[listing_a, listing_b],
            run_id="test-cache",
        )
        _patch_settings(tmp_path)
        result = await job_analysis_node(state, llm_client=mock_client)

        assert len(mock_client.calls) == 1
        assert "same_key" in result.job_analyses

    @pytest.mark.asyncio
    async def test_cache_invalidates_when_description_changes(self, tmp_path: Path) -> None:
        from pipelines.job_agent.nodes.job_analysis import job_analysis_node

        mock_client = MockLLMClient(responses=[_mock_analysis_json(), _mock_analysis_json()])
        listing_a = _make_listing(dedup_key="same_key", description="first description")
        listing_b = _make_listing(dedup_key="same_key", description="second description changed")
        state = JobAgentState(
            search_criteria=SearchCriteria(),
            qualified_listings=[listing_a, listing_b],
            run_id="test-cache-desc-change",
        )
        _patch_settings(tmp_path)
        result = await job_analysis_node(state, llm_client=mock_client)

        assert len(mock_client.calls) == 2
        assert "same_key" in result.job_analyses

    @pytest.mark.asyncio
    async def test_sets_analyzed_status_on_success(self, tmp_path: Path) -> None:
        from pipelines.job_agent.nodes.job_analysis import job_analysis_node

        mock_client = MockLLMClient(responses=[_mock_analysis_json()])
        listing = _make_listing()
        state = JobAgentState(
            search_criteria=SearchCriteria(),
            qualified_listings=[listing],
            run_id="test-status",
        )
        _patch_settings(tmp_path)
        await job_analysis_node(state, llm_client=mock_client)
        assert listing.status == ApplicationStatus.ANALYZED

    @pytest.mark.asyncio
    async def test_uses_full_description(self, tmp_path: Path) -> None:
        """Verify the node sends the full JD, not a truncated slice."""
        from pipelines.job_agent.nodes.job_analysis import job_analysis_node

        long_desc = "A" * 10_000
        mock_client = MockLLMClient(responses=[_mock_analysis_json()])
        state = JobAgentState(
            search_criteria=SearchCriteria(),
            qualified_listings=[_make_listing(description=long_desc)],
            run_id="test-full-jd",
        )
        _patch_settings(tmp_path)
        await job_analysis_node(state, llm_client=mock_client)

        prompt_sent = mock_client.calls[0][0]
        assert "A" * 10_000 in prompt_sent

    @pytest.mark.asyncio
    async def test_model_passed_from_config(self, tmp_path: Path) -> None:
        from pipelines.job_agent.nodes.job_analysis import job_analysis_node

        mock_client = MockLLMClient(responses=[_mock_analysis_json()])
        state = JobAgentState(
            search_criteria=SearchCriteria(),
            qualified_listings=[_make_listing()],
            run_id="test-model",
        )
        _patch_settings(tmp_path, model="claude-haiku-4-5-20251001")
        await job_analysis_node(state, llm_client=mock_client)

        assert mock_client.calls[0][1]["model"] == "claude-haiku-4-5-20251001"


class TestAnalysisSystemPromptCacheability:
    """Regression guard for prompt-cache cost savings.

    The Anthropic prefix cache requires a minimum prefix size per model.
    Empirically verified against the live Haiku 4.5 API on 2026-04-14:
    the actual threshold is ~4,096 tokens (not the 2,048 listed in the
    public docs, which track an older Haiku version). Prompts below
    ~4,096 tokens produce ``cache_hit: null`` with zero creation and
    zero read — silently no-op caching.

    The production run of 2026-04-14 produced ``cache_hit: null`` across
    all 28 Haiku calls at ~2,449 combined tokens. Fix: expand the system
    prompt so the combined system + schema is comfortably above 4,096
    tokens with headroom for minor wording edits.
    """

    def test_analysis_system_exceeds_haiku_cache_minimum(self) -> None:
        from pipelines.job_agent.models.resume_tailoring import JobAnalysisResult
        from pipelines.job_agent.nodes.job_analysis import _ANALYSIS_SYSTEM

        schema_chars = len(
            json.dumps(JobAnalysisResult.model_json_schema(), indent=2)
        )
        combined_chars = len(_ANALYSIS_SYSTEM) + schema_chars + 200  # boilerplate headroom
        # English prose tokenises at roughly 3.7 chars/token on Claude's
        # tokenizer. Use the conservative 4 chars/token for the floor
        # estimate and demand a comfortable buffer above the empirically
        # observed Haiku 4.5 threshold (~4,096 tokens).
        approx_tokens = combined_chars // 4
        assert approx_tokens >= 4200, (
            f"Combined system prompt + schema is ~{approx_tokens} tokens, "
            "below the Haiku 4.5 prefix cache minimum (~4,096 tokens, "
            "empirically verified). Expand _ANALYSIS_SYSTEM with additional "
            "stable, useful content so prompt caching engages and cuts "
            "per-call cost. The 2026-04-14 production run burned ~$0.19 on "
            "uncached prefill that would have been near-zero with caching."
        )
