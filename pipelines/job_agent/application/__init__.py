"""Job application automation pipeline node.

Orchestrates an LLM-driven web agent to fill out job applications.
The agent observes each form page, maps field labels to candidate
profile answers, and fills the form — pausing for human approval
before final submission.

This package uses:
- ``core.web_agent.controller.WebAgentController`` for the observe-act loop
- ``core.browser.actions.BrowserActions`` for stealth-wrapped page interaction
- ``core.browser.observer.PageObserver`` for structured page state extraction
- ``core.llm.structured.structured_complete`` for the QA answerer
"""
