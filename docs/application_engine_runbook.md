# Application Engine Runbook

## Overview
The Application Engine is responsible for autonomously submitting job applications to various ATS (Applicant Tracking System) platforms, including API-based platforms like Greenhouse and Lever, and browser-based template systems like Ashby or Workday.

## Core Components
- **API Submitters**: Located in `pipelines/job_agent/application/submitters/`. These components use the requests library to interface with public APIs. (e.g., `greenhouse_api.py`, `lever_api.py`).
- **Template Submitters**: Use Playwright and the `core.browser` primitives to fill out single or multi-page forms (e.g., `ashby.py`, `linkedin_easy_apply.py`).
- **QA Answerer**: Located in `pipelines/job_agent/application/qa_answerer.py`. Uses an LLM to generate custom answers for text boxes based on the user's application profile.
- **Field Mapper**: Maps simple identifiable fields deterministically.

## Troubleshooting

### 1. Application is Stuck in "Awaiting Review"
This is intended behavior when the `dry_run` flag is enabled or for manual human-in-the-loop review.
**Action**:
- Check the screenshot path returned in the `ApplicationAttempt`.
- If the form looks correct, manually approve it or click submit.
- If you want automatic submission, ensure `dry_run` is set to `False` and human approval is disabled (not recommended for browser templates until high confidence is established).

### 2. Playwright Selectors Failing
When an ATS updates its UI, hardcoded selectors may break.
**Action**:
- Check the `ApplicationAttempt` error logs.
- Review the `capture_application_failure` screenshot and HTML dump.
- Update selectors in the template file (e.g., `pipelines/job_agent/application/templates/ashby.py`). Provide fallback selectors using Playwright's `,` separated selectors for resilience.

### 3. LLM Rate Limits or Errors
If `qa_answerer` fails due to API limits.
**Action**:
- The pipeline uses a `post_with_backoff` logic for direct API. For LLM rate limits, the `core.llm` client should handle retries.
- Review the run-scoped `QACache` logic if repetitive questions are overwhelming the API limits.

### 4. Deterministic Field Mapping Missing
If a common field (e.g., Last Name) is being sent to the LLM.
**Action**:
- Add the field name variation to `_FIELD_PATTERNS` inside `pipelines/job_agent/application/field_mapper.py`.
- This ensures it's resolved rapidly without LLM token cost.

## Testing & Validation
To test modifications safely, utilize the existing pytest framework:
```bash
pytest pipelines/job_agent/tests/test_ashby_template.py
pytest pipelines/job_agent/tests/test_application_qa_answerer.py
pytest core/tests/test_web_agent_actions.py
```
Ensure that no real external calls are made during tests by mocking HTTP clients, `Playwright Page`, and the `LLMClient`.
