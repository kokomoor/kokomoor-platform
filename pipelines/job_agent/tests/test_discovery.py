"""Tests for the Discovery node."""

from __future__ import annotations

import pytest

from pipelines.job_agent.nodes.discovery import _generate_dedup_key, discovery_node
from pipelines.job_agent.state import JobAgentState, PipelinePhase, SearchCriteria


class TestDedupKey:
    """Tests for deduplication key generation."""

    def test_deterministic(self) -> None:
        """Same inputs always produce the same key."""
        key1 = _generate_dedup_key("Anduril", "TPM", "https://example.com/1")
        key2 = _generate_dedup_key("Anduril", "TPM", "https://example.com/1")
        assert key1 == key2

    def test_case_insensitive(self) -> None:
        """Keys are case-insensitive for company and title."""
        key1 = _generate_dedup_key("Anduril", "TPM", "https://example.com/1")
        key2 = _generate_dedup_key("anduril", "tpm", "https://example.com/1")
        assert key1 == key2

    def test_different_urls_different_keys(self) -> None:
        """Different URLs produce different keys."""
        key1 = _generate_dedup_key("Anduril", "TPM", "https://example.com/1")
        key2 = _generate_dedup_key("Anduril", "TPM", "https://example.com/2")
        assert key1 != key2


class TestDiscoveryNode:
    """Tests for the discovery node."""

    @pytest.mark.asyncio
    async def test_sets_phase(self) -> None:
        """Discovery node sets the pipeline phase correctly."""
        state = JobAgentState(search_criteria=SearchCriteria())
        result = await discovery_node(state)
        assert result.phase == PipelinePhase.DISCOVERY

    @pytest.mark.asyncio
    async def test_returns_state_with_listings(self) -> None:
        """Discovery node populates discovered_listings (empty for stub)."""
        state = JobAgentState(search_criteria=SearchCriteria())
        result = await discovery_node(state)
        assert isinstance(result.discovered_listings, list)
