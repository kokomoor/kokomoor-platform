"""LangGraph state machine for the job application pipeline.

Defines the directed graph of nodes and edges that constitute the
pipeline. Each node is a function that receives ``JobAgentState``,
performs work, and returns the modified state.

Usage:
    from pipelines.job_agent.graph import build_graph

    graph = build_graph()
    result = await graph.ainvoke(initial_state)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from langgraph.graph import END
from langgraph.graph.state import CompiledStateGraph, StateGraph

from pipelines.job_agent.nodes.discovery import discovery_node
from pipelines.job_agent.nodes.filtering import filtering_node
from pipelines.job_agent.nodes.job_analysis import job_analysis_node
from pipelines.job_agent.nodes.manual_extraction import manual_extraction_node
from pipelines.job_agent.nodes.notification import notification_node
from pipelines.job_agent.nodes.tailoring import tailoring_node
from pipelines.job_agent.nodes.tracking import tracking_node
from pipelines.job_agent.state import JobAgentState

if TYPE_CHECKING:
    from core.llm.protocol import LLMClient

logger = structlog.get_logger(__name__)


def _should_continue_after_filtering(state: JobAgentState) -> str:
    """Route after filtering: proceed to job_analysis or skip to notification."""
    if not state.qualified_listings:
        logger.info("no_qualified_listings", total_discovered=len(state.discovered_listings))
        return "notification"
    return "job_analysis"


def _should_continue_after_manual_extraction(state: JobAgentState) -> str:
    """Route after manual extraction: analyse if listing was extracted."""
    if not state.qualified_listings:
        logger.info("manual_extract_empty", errors=len(state.errors))
        return "notification"
    return "job_analysis"


def _should_continue_after_job_analysis(state: JobAgentState) -> str:
    """Route after job analysis: tailor if at least one analysis succeeded."""
    if not state.job_analyses:
        logger.info("job_analysis_empty", errors=len(state.errors))
        return "notification"
    return "tailoring"


def _should_continue_after_review(state: JobAgentState) -> str:
    """Route after human review: apply approved listings or skip."""
    if not state.approved_listings:
        return "notification"
    return "application"


def build_graph(
    *,
    llm_client: LLMClient | None = None,
) -> CompiledStateGraph[JobAgentState, None, JobAgentState, JobAgentState]:
    """Construct and compile the job agent LangGraph.

    Args:
        llm_client: Optional LLM client for LLM-backed nodes.
            Defaults to ``AnthropicClient()`` at runtime if not provided.
            Pass ``MockLLMClient`` in tests.

    Returns a compiled graph ready for ``ainvoke()`` or ``astream()``.

    The graph implements this flow::

        START → discovery → filtering → job_analysis → tailoring
              → tracking → notification → END

    With conditional edges that skip stages when there's nothing to
    process (e.g., no qualified listings skip job_analysis entirely).
    """
    graph: StateGraph[JobAgentState, None, JobAgentState, JobAgentState] = StateGraph(
        JobAgentState,
    )

    async def _analysis_wrapper(state: JobAgentState) -> JobAgentState:
        return await job_analysis_node(state, llm_client=llm_client)

    async def _tailoring_wrapper(state: JobAgentState) -> JobAgentState:
        return await tailoring_node(state, llm_client=llm_client)

    graph.add_node("discovery", discovery_node)
    graph.add_node("filtering", filtering_node)
    graph.add_node("job_analysis", _analysis_wrapper)
    graph.add_node("tailoring", _tailoring_wrapper)
    graph.add_node("tracking", tracking_node)
    graph.add_node("notification", notification_node)

    graph.set_entry_point("discovery")
    graph.add_edge("discovery", "filtering")

    graph.add_conditional_edges(
        "filtering",
        _should_continue_after_filtering,
        {
            "job_analysis": "job_analysis",
            "notification": "notification",
        },
    )

    graph.add_conditional_edges(
        "job_analysis",
        _should_continue_after_job_analysis,
        {
            "tailoring": "tailoring",
            "notification": "notification",
        },
    )

    graph.add_edge("tailoring", "tracking")
    graph.add_edge("tracking", "notification")
    graph.add_edge("notification", END)

    return graph.compile()


def build_manual_graph(
    *,
    llm_client: LLMClient | None = None,
) -> CompiledStateGraph[JobAgentState, None, JobAgentState, JobAgentState]:
    """Construct a truncated graph for direct manual job URLs.

    Flow:
        START -> manual_extraction -> job_analysis -> tailoring
              -> tracking -> notification -> END
    """
    graph: StateGraph[JobAgentState, None, JobAgentState, JobAgentState] = StateGraph(
        JobAgentState,
    )

    async def _analysis_wrapper(state: JobAgentState) -> JobAgentState:
        return await job_analysis_node(state, llm_client=llm_client)

    async def _tailoring_wrapper(state: JobAgentState) -> JobAgentState:
        return await tailoring_node(state, llm_client=llm_client)

    graph.add_node("manual_extraction", manual_extraction_node)
    graph.add_node("job_analysis", _analysis_wrapper)
    graph.add_node("tailoring", _tailoring_wrapper)
    graph.add_node("tracking", tracking_node)
    graph.add_node("notification", notification_node)

    graph.set_entry_point("manual_extraction")
    graph.add_conditional_edges(
        "manual_extraction",
        _should_continue_after_manual_extraction,
        {
            "job_analysis": "job_analysis",
            "notification": "notification",
        },
    )
    graph.add_conditional_edges(
        "job_analysis",
        _should_continue_after_job_analysis,
        {
            "tailoring": "tailoring",
            "notification": "notification",
        },
    )
    graph.add_edge("tailoring", "tracking")
    graph.add_edge("tracking", "notification")
    graph.add_edge("notification", END)

    return graph.compile()
