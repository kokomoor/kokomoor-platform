import pytest
from unittest.mock import AsyncMock, MagicMock
from pipelines.job_agent.application.node import _handle_attempt_outcome
from pipelines.job_agent.models import JobListing, ApplicationAttempt
from pipelines.job_agent.state import JobAgentState

@pytest.mark.asyncio
async def test_handle_attempt_outcome_metrics():
    # Reset metrics if needed (Prometheus doesn't easily reset, we just check relative)
    from core.observability.metrics import APPLICATION_ATTEMPTS
    
    # Capture initial value
    try:
        initial = APPLICATION_ATTEMPTS.labels(
            platform="greenhouse", strategy="api_greenhouse", status="submitted"
        )._value.get()
    except Exception:
        initial = 0
    
    listing = JobListing(title="T", company="C", location="L", url="U", dedup_key="K")
    attempt = ApplicationAttempt(
        dedup_key="K",
        status="submitted",
        strategy="api_greenhouse",
        summary="S",
        fields_filled=5,
        llm_calls_made=2
    )
    
    state = JobAgentState(run_id="run1")
    dedup_store = AsyncMock()
    
    await _handle_attempt_outcome(state, listing, attempt, dedup_store)
    
    final = APPLICATION_ATTEMPTS.labels(
        platform="greenhouse", strategy="api_greenhouse", status="submitted"
    )._value.get()
    
    assert final == initial + 1
    
    # Check other metrics
    from core.observability.metrics import APPLICATION_FIELDS_FILLED, APPLICATION_LLM_QA_CALLS
    # We just check they don't crash
    assert APPLICATION_FIELDS_FILLED.labels(platform="greenhouse")._value.get() >= 5
    assert APPLICATION_LLM_QA_CALLS.labels(platform="greenhouse")._value.get() >= 2
