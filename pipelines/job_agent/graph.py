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

import structlog
from langgraph.graph import END
from langgraph.graph.state import CompiledStateGraph, StateGraph

from pipelines.job_agent.nodes.discovery import discovery_node
from pipelines.job_agent.nodes.filtering import filtering_node
from pipelines.job_agent.nodes.notification import notification_node
from pipelines.job_agent.nodes.tracking import tracking_node
from pipelines.job_agent.state import JobAgentState

logger = structlog.get_logger(__name__)


def _should_continue_after_filtering(state: JobAgentState) -> str:
    """Route after filtering: proceed to tailoring or skip to notification."""
    if not state.qualified_listings:
        logger.info("no_qualified_listings", total_discovered=len(state.discovered_listings))
        return "notification"
    return "tailoring"


def _should_continue_after_review(state: JobAgentState) -> str:
    """Route after human review: apply approved listings or skip."""
    if not state.approved_listings:
        return "notification"
    return "application"


def build_graph() -> CompiledStateGraph[JobAgentState, None, JobAgentState, JobAgentState]:
    """Construct and compile the job agent LangGraph.

    Returns a compiled graph ready for ``ainvoke()`` or ``astream()``.

    The graph implements this flow::

        START → discovery → filtering → tailoring → human_review
              → application → tracking → notification → END

    With conditional edges that skip stages when there's nothing to
    process (e.g., no qualified listings skip tailoring entirely).
    """
    graph: StateGraph[JobAgentState, None, JobAgentState, JobAgentState] = StateGraph(
        JobAgentState,
    )

    # Register nodes.
    graph.add_node("discovery", discovery_node)
    graph.add_node("filtering", filtering_node)
    # TODO: Milestone 3 — uncomment when tailoring node is implemented.
    # graph.add_node("tailoring", tailoring_node)
    # TODO: Milestone 4 — uncomment when these nodes are implemented.
    # graph.add_node("human_review", human_review_node)
    # graph.add_node("application", application_node)
    graph.add_node("tracking", tracking_node)
    graph.add_node("notification", notification_node)

    # Define edges.
    graph.set_entry_point("discovery")
    graph.add_edge("discovery", "filtering")

    # Conditional: filtering → tailoring (if listings) or notification (if empty).
    graph.add_conditional_edges(
        "filtering",
        _should_continue_after_filtering,
        {
            "tailoring": "tracking",  # Temporarily route to tracking until tailoring exists.
            "notification": "notification",
        },
    )

    graph.add_edge("tracking", "notification")
    graph.add_edge("notification", END)

    return graph.compile()
