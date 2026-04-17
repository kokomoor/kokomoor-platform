import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from pipelines.job_agent.application.qa_answerer import (
    answer_application_question,
    _is_generic_question,
    QACache,
    FormFieldAnswer,
)

@pytest.fixture
def mock_llm_client():
    client = MagicMock()
    # Mock structured_complete
    return client

def test_is_generic_question():
    assert _is_generic_question("Are you authorized to work in the US?") is True
    assert _is_generic_question("Do you require sponsorship?") is True
    assert _is_generic_question("Why do you want to work here?") is False
    assert _is_generic_question("What is your gender?") is True

def test_qa_cache():
    cache = QACache()
    answer = FormFieldAnswer(answer="Yes", confidence=1.0, source="authorization")
    cache.set("key1", answer)
    
    cached = cache.get("key1")
    assert cached is not None
    assert cached.answer == "Yes"
    
    cache.clear()
    assert cache.get("key1") is None

@pytest.mark.asyncio
@patch("pipelines.job_agent.application.qa_answerer.structured_complete", new_callable=AsyncMock)
async def test_answer_application_question_generic_cached(mock_structured_complete):
    mock_llm = MagicMock()
    
    answer = FormFieldAnswer(answer="Yes", confidence=1.0, source="authorization")
    mock_structured_complete.return_value = answer
    
    cache = QACache()
    
    # First call - cache miss
    result1 = await answer_application_question(
        llm=mock_llm,
        field_label="Are you authorized to work in the US?",
        field_type="radio",
        candidate_profile='{"authorization": {"authorized_us": true}}',
        cache=cache
    )
    
    assert result1.answer == "Yes"
    assert result1.used_cache is False
    assert mock_structured_complete.call_count == 1
    
    # Second call - cache hit
    result2 = await answer_application_question(
        llm=mock_llm,
        field_label="Are you authorized to work in the US?",
        field_type="radio",
        candidate_profile='{"authorization": {"authorized_us": true}}',
        cache=cache
    )
    
    assert result2.answer == "Yes"
    assert result2.used_cache is True
    assert mock_structured_complete.call_count == 1 # Still 1

@pytest.mark.asyncio
@patch("pipelines.job_agent.application.qa_answerer.structured_complete", new_callable=AsyncMock)
async def test_answer_application_question_specific_not_cached(mock_structured_complete):
    mock_llm = MagicMock()
    
    answer = FormFieldAnswer(answer="I love this company", confidence=1.0, source="cover_letter")
    mock_structured_complete.return_value = answer
    
    cache = QACache()
    
    # First call
    result1 = await answer_application_question(
        llm=mock_llm,
        field_label="Why do you want to work here?",
        field_type="textarea",
        candidate_profile="{}",
        cache=cache
    )
    
    assert result1.answer == "I love this company"
    assert result1.used_cache is False
    assert mock_structured_complete.call_count == 1
    
    # Second call - should not hit cache because it's not generic
    result2 = await answer_application_question(
        llm=mock_llm,
        field_label="Why do you want to work here?",
        field_type="textarea",
        candidate_profile="{}",
        cache=cache
    )
    
    assert result2.answer == "I love this company"
    assert result2.used_cache is False
    assert mock_structured_complete.call_count == 2
