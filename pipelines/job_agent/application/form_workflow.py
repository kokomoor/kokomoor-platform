"""Form-fill workflow — drives the web agent through a job application.

Orchestration flow:
1. Open the application URL in a managed browser.
2. Construct an ``AgentGoal`` with ``require_human_approval_before=["submit"]``.
3. The web agent observes each page, fills fields using the QA answerer,
   and navigates through multi-step forms.
4. At the submit step, the agent pauses and returns control to the caller
   for human review.
5. After approval, call ``resume()`` to complete submission.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from core.browser.actions import BrowserActions
from core.browser.human_behavior import HumanBehavior
from core.browser.observer import PageObserver
from core.web_agent.controller import WebAgentController
from core.web_agent.protocol import AgentGoal, AgentResult

if TYPE_CHECKING:
    from playwright.async_api import Page

    from core.llm.protocol import LLMClient

logger = structlog.get_logger(__name__)

_FORM_AGENT_SYSTEM = (
    "You are filling out a job application form. Follow these rules:\n"
    "1. Read each field carefully before filling it.\n"
    "2. For text fields, use the candidate profile information.\n"
    "3. For select/radio fields, choose the best matching option.\n"
    "4. If you see a file upload for resume, use the upload action.\n"
    "5. Navigate 'Next' / 'Continue' buttons to advance through steps.\n"
    "6. When you reach a 'Submit' or 'Apply' button, use action='done'.\n"
    "7. If you encounter an error, try to correct the field and retry.\n"
    "8. If you are truly stuck (e.g. CAPTCHA, login wall), use action='stuck'."
)


async def fill_application(
    page: Page,
    llm: LLMClient,
    *,
    application_url: str,
    candidate_profile: str,
    resume_path: str | None = None,
    run_id: str = "",
) -> AgentResult:
    """Drive the web agent to fill out a job application.

    Args:
        page: A Playwright page from ``BrowserManager``.
        llm: The LLM client for the agent and QA answerer.
        application_url: URL of the application form.
        candidate_profile: Candidate profile as YAML text.
        resume_path: Optional path to a resume file for upload fields.
        run_id: Pipeline run identifier.

    Returns:
        An ``AgentResult``. If ``status == "awaiting_approval"``, call
        ``controller.resume(approved=True)`` after human review.
    """
    behavior = HumanBehavior()
    actions = BrowserActions(page, behavior)
    observer = PageObserver()

    nav = await actions.goto(application_url)
    if not nav.success:
        return AgentResult(
            status="error",
            summary=f"Failed to navigate to application: {nav.error}",
        )

    goal = AgentGoal(
        instruction=(
            "Fill out this job application completely using the candidate "
            "profile provided. Navigate through all form pages. When you "
            "reach the final submit button, report done so a human can review."
        ),
        success_signals=["application submitted", "thank you for applying", "confirmation"],
        failure_signals=["access denied", "page not found", "404"],
        max_steps=50,
        require_human_approval_before=[],
    )

    context_info = f"Candidate profile:\n{candidate_profile}"
    if resume_path:
        context_info += f"\nResume file: {resume_path}"

    controller = WebAgentController(
        page,
        llm,
        goal=goal,
        actions=actions,
        observer=observer,
        system_prompt=f"{_FORM_AGENT_SYSTEM}\n\n{context_info}",
        run_id=run_id,
    )

    result = await controller.run()
    logger.info(
        "application.workflow_complete",
        status=result.status,
        steps=result.steps_taken,
        url=result.final_url,
    )
    return result
