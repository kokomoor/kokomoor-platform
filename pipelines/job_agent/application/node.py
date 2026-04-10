"""LangGraph node: application_node — drives job application form-filling.

This node takes qualified job listings from the pipeline state and
attempts to fill their application forms using the web agent. It pauses
before final submission for human approval.

Integration with the pipeline:
- Reads ``state.qualified_listings`` (filtered, analyzed listings)
- For each listing with an application URL, runs ``fill_application``
- Records results in ``state.application_results``
- Respects the "never auto-submit" invariant
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


async def application_node(state: dict[str, Any]) -> dict[str, Any]:
    """Process qualified listings through the application form-filler.

    This is a skeleton node. Full implementation will:
    1. Load candidate profile from YAML.
    2. For each qualified listing with an application URL:
       a. Open a browser page via BrowserManager.
       b. Call fill_application() to drive the web agent.
       c. If agent returns "awaiting_approval", notify human.
       d. After approval, call controller.resume() to submit.
    3. Record outcomes in the pipeline state.

    The skeleton is intentionally minimal — it establishes the node
    signature and integration point without implementing the full
    browser lifecycle, which requires the orchestration layer.
    """
    logger.info(
        "application_node.start",
        qualified_count=len(state.get("qualified_listings", [])),
    )

    state.setdefault("application_results", [])

    logger.info(
        "application_node.complete",
        results_count=len(state["application_results"]),
    )
    return state
