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

from typing import TYPE_CHECKING, Any, cast

import structlog
from langgraph.graph import END
from langgraph.graph.state import CompiledStateGraph, StateGraph

from pipelines.job_agent.models import ApplicationStatus
from pipelines.job_agent.nodes.bulk_extraction import bulk_extraction_node
from pipelines.job_agent.nodes.cover_letter_tailoring import cover_letter_tailoring_node
from pipelines.job_agent.nodes.discovery import discovery_node
from pipelines.job_agent.nodes.filtering import filtering_node
from pipelines.job_agent.nodes.job_analysis import job_analysis_node
from pipelines.job_agent.nodes.manual_extraction import manual_extraction_node
from pipelines.job_agent.nodes.notification import notification_node
from pipelines.job_agent.nodes.ranking import ranking_node
from pipelines.job_agent.nodes.tailoring import tailoring_node
from pipelines.job_agent.nodes.tracking import tracking_node
from pipelines.job_agent.state import JobAgentState

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from core.llm.protocol import LLMClient

logger = structlog.get_logger(__name__)


def _should_continue_after_filtering(state: JobAgentState) -> str:
    """Route after filtering: proceed to bulk_extraction or skip to notification."""
    if not state.qualified_listings:
        logger.info("no_qualified_listings", total_discovered=len(state.discovered_listings))
        return "notification"
    return "bulk_extraction"


def _should_continue_after_bulk_extraction(state: JobAgentState) -> str:
    """Route after bulk extraction: proceed if any listing has a description."""
    extractable = [
        listing
        for listing in state.qualified_listings
        if listing.status != ApplicationStatus.ERRORED and listing.description
    ]
    if not extractable:
        logger.info(
            "bulk_extraction_all_failed",
            total=len(state.qualified_listings),
        )
        return "notification"
    return "job_analysis"


def _should_continue_after_manual_extraction(state: JobAgentState) -> str:
    """Route after manual extraction: analyse if listing was extracted."""
    if not state.qualified_listings:
        logger.info("manual_extract_empty", errors=len(state.errors))
        return "notification"
    return "job_analysis"


def _should_continue_after_job_analysis(state: JobAgentState) -> str:
    """Route after job analysis: rank+tailor if at least one analysis succeeded."""
    if not state.job_analyses:
        logger.info("job_analysis_empty", errors=len(state.errors))
        return "notification"
    return "ranking"


def _llm_node_wrapper(
    node_fn: Callable[..., Coroutine[Any, Any, JobAgentState]],
    *,
    llm_client: LLMClient | None,
) -> Callable[[JobAgentState], Coroutine[Any, Any, JobAgentState]]:
    """Wrap an LLM-backed node with optional injected client."""

    async def _wrapped(state: JobAgentState) -> JobAgentState:
        return await node_fn(state, llm_client=llm_client)

    return _wrapped


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

        START → discovery → filtering → bulk_extraction → job_analysis
              → tailoring → cover_letter_tailoring
              → tracking → notification → END

    With conditional edges that skip stages when there's nothing to
    process (e.g., no qualified listings skip to notification).
    """
    graph: StateGraph[JobAgentState, None, JobAgentState, JobAgentState] = StateGraph(
        JobAgentState,
    )

    graph.add_node("discovery", discovery_node)
    graph.add_node("filtering", filtering_node)
    graph.add_node("bulk_extraction", bulk_extraction_node)
    graph.add_node(
        "job_analysis",
        cast(Any, _llm_node_wrapper(job_analysis_node, llm_client=llm_client)),  # noqa: TC006
    )
    graph.add_node(
        "tailoring",
        cast(Any, _llm_node_wrapper(tailoring_node, llm_client=llm_client)),  # noqa: TC006
    )
    graph.add_node(
        "cover_letter_tailoring",
        cast(
            "Any",
            _llm_node_wrapper(cover_letter_tailoring_node, llm_client=llm_client),
        ),
    )
    graph.add_node("ranking", ranking_node)
    graph.add_node("tracking", tracking_node)
    graph.add_node("notification", notification_node)

    graph.set_entry_point("discovery")
    graph.add_edge("discovery", "filtering")

    graph.add_conditional_edges(
        "filtering",
        _should_continue_after_filtering,
        {
            "bulk_extraction": "bulk_extraction",
            "notification": "notification",
        },
    )

    graph.add_conditional_edges(
        "bulk_extraction",
        _should_continue_after_bulk_extraction,
        {
            "job_analysis": "job_analysis",
            "notification": "notification",
        },
    )

    graph.add_conditional_edges(
        "job_analysis",
        _should_continue_after_job_analysis,
        {
            "ranking": "ranking",
            "notification": "notification",
        },
    )

    graph.add_edge("ranking", "tailoring")

    graph.add_edge("tailoring", "cover_letter_tailoring")
    graph.add_edge("cover_letter_tailoring", "tracking")
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
              -> cover_letter_tailoring
              -> tracking -> notification -> END
    """
    graph: StateGraph[JobAgentState, None, JobAgentState, JobAgentState] = StateGraph(
        JobAgentState,
    )

    graph.add_node("manual_extraction", manual_extraction_node)
    graph.add_node(
        "job_analysis",
        cast(Any, _llm_node_wrapper(job_analysis_node, llm_client=llm_client)),  # noqa: TC006
    )
    graph.add_node(
        "tailoring",
        cast(Any, _llm_node_wrapper(tailoring_node, llm_client=llm_client)),  # noqa: TC006
    )
    graph.add_node(
        "cover_letter_tailoring",
        cast(
            "Any",
            _llm_node_wrapper(cover_letter_tailoring_node, llm_client=llm_client),
        ),
    )
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
    graph.add_edge("tailoring", "cover_letter_tailoring")
    graph.add_edge("cover_letter_tailoring", "tracking")
    graph.add_edge("tracking", "notification")
    graph.add_edge("notification", END)

    return graph.compile()
