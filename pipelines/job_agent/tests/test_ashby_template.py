import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from pipelines.job_agent.models import JobListing, CandidateApplicationProfile
from pipelines.job_agent.application.submitters.template_ashby import fill_ashby_application
from pipelines.job_agent.application.qa_answerer import FormFieldAnswer

@pytest.fixture
def mock_page():
    page = AsyncMock()
    return page

@pytest.fixture
def mock_behavior():
    behavior = MagicMock()
    behavior.reading_pause = AsyncMock()
    behavior.human_click = AsyncMock()
    behavior.type_with_cadence = AsyncMock()
    return behavior

@pytest.fixture
def dummy_profile():
    return CandidateApplicationProfile.model_validate({
        "schema_version": 1,
        "personal": {
            "first_name": "Test",
            "last_name": "User",
            "email": "test@example.com",
            "phone": "1234567890",
            "phone_formatted": "(123) 456-7890",
            "linkedin_url": "",
            "github_url": "",
            "portfolio_url": "",
            "website_url": ""
        },
        "address": {"street": "", "city": "", "state": "", "zip": "", "country": ""},
        "authorization": {"authorized_us": True, "require_sponsorship": False, "citizenship": "", "clearance": ""},
        "demographics": {"gender": "", "race_ethnicity": "", "veteran_status": "", "disability_status": ""},
        "education": {"highest_degree": "", "school": "", "graduation_year": "", "gpa": "", "field_of_study": "", "additional_degrees": []},
        "screening": {"years_experience": "", "willing_to_relocate": True, "desired_salary": "", "earliest_start_date": "", "how_did_you_hear": "", "referral_name": "", "languages_spoken": []},
        "source": {"default": "", "linkedin": "", "greenhouse": "", "lever": "", "indeed": ""}
    })

@pytest.fixture
def dummy_listing():
    return JobListing(
        url="https://jobs.ashbyhq.com/example/123",
        title="Test Job",
        company="Test Company",
        dedup_key="test_job_123"
    )

@pytest.mark.asyncio
async def test_fill_ashby_application_no_page(dummy_listing, dummy_profile):
    with pytest.raises(ValueError, match="Page is required"):
        await fill_ashby_application(dummy_listing, dummy_profile, Path("resume.pdf"), None)

@pytest.mark.asyncio
@patch("pipelines.job_agent.application.submitters.template_ashby.map_field")
@patch("pipelines.job_agent.application.submitters.template_ashby.get_field_label", new_callable=AsyncMock, return_value="First Name")
@patch("pipelines.job_agent.application._debug.capture_application_failure", new_callable=AsyncMock, return_value="screenshot.png")
async def test_fill_ashby_application_success(mock_capture, mock_get_label, mock_map_field, mock_page, mock_behavior, dummy_listing, dummy_profile):
    from pipelines.job_agent.application.field_mapper import FieldMapping
    mock_map_field.return_value = FieldMapping(value="MappedValue", confidence=1.0, source="personal")
    
    # Mock field elements
    mock_el = AsyncMock()
    mock_el.is_visible.return_value = True
    mock_el.is_disabled.return_value = False
    mock_el.evaluate.return_value = "input"
    mock_el.get_attribute.return_value = "text"
    
    mock_page.query_selector_all.return_value = [mock_el]
    
    result = await fill_ashby_application(
        listing=dummy_listing,
        profile=dummy_profile,
        resume_path=Path("resume.pdf"),
        cover_letter_path=None,
        page=mock_page,
        behavior=mock_behavior
    )
    
    assert result.status == "awaiting_review"
    assert result.fields_filled == 1
    assert result.llm_calls_made == 0
    assert result.screenshot_path == "screenshot.png"
    
    mock_el.evaluate.assert_any_call("el => el.value = ''")
    mock_behavior.type_with_cadence.assert_called_once_with(mock_el, "MappedValue")

@pytest.mark.asyncio
@patch("pipelines.job_agent.application.submitters.template_ashby.map_field")
@patch("pipelines.job_agent.application.submitters.template_ashby.answer_application_question", new_callable=AsyncMock)
@patch("pipelines.job_agent.application.submitters.template_ashby.get_field_label", new_callable=AsyncMock, return_value="Custom Question")
@patch("pipelines.job_agent.application._debug.capture_application_failure", new_callable=AsyncMock, return_value="screenshot.png")
async def test_fill_ashby_application_llm_fallback(mock_capture, mock_get_label, mock_answer, mock_map_field, mock_page, mock_behavior, dummy_listing, dummy_profile):
    from pipelines.job_agent.application.field_mapper import FieldMapping
    mock_map_field.return_value = FieldMapping(value="", confidence=0.0, source="unmapped")
    
    mock_answer.return_value = FormFieldAnswer(answer="LLM Answer", confidence=1.0, source="qa")
    
    # Mock field elements
    mock_el = AsyncMock()
    mock_el.is_visible.return_value = True
    mock_el.is_disabled.return_value = False
    mock_el.evaluate.return_value = "input"
    mock_el.get_attribute.return_value = "text"
    
    mock_page.query_selector_all.return_value = [mock_el]
    
    mock_llm = MagicMock()
    
    result = await fill_ashby_application(
        listing=dummy_listing,
        profile=dummy_profile,
        resume_path=Path("resume.pdf"),
        cover_letter_path=None,
        page=mock_page,
        llm=mock_llm,
        behavior=mock_behavior
    )
    
    assert result.status == "awaiting_review"
    assert result.fields_filled == 1
    assert result.llm_calls_made == 1
    
    mock_answer.assert_called_once()
    mock_behavior.type_with_cadence.assert_called_once_with(mock_el, "LLM Answer")
