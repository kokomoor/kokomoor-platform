import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from pipelines.job_agent.application.agent_filler import _build_agent_system_prompt, fill_application_with_agent
from pipelines.job_agent.models import CandidateApplicationProfile, JobListing, ApplicationAttempt
from core.browser.actions import ActionResult
from core.web_agent.protocol import AgentResult

AgentResult.model_rebuild()

from pipelines.job_agent.models.application import (
    CandidateApplicationProfile, PersonalInfo, AddressInfo, 
    AuthorizationInfo, DemographicInfo, EducationInfo, 
    ScreeningInfo, SourceTracking
)

@pytest.fixture
def mock_profile():
    return CandidateApplicationProfile(
        personal=PersonalInfo(
            first_name="Sam",
            last_name="Kokomoor",
            email="sam@example.com",
            phone="+11234567890",
            phone_formatted="(123) 456-7890",
            linkedin_url="linkedin.com/in/sam",
            github_url="github.com/sam"
        ),
        address=AddressInfo(city="Boston", state="MA"),
        authorization=AuthorizationInfo(
            authorized_us=True,
            require_sponsorship=False,
            clearance="Secret"
        ),
        demographics=DemographicInfo(
            gender="Male",
            race_ethnicity="White",
            veteran_status="No",
            disability_status="No"
        ),
        education=EducationInfo(
            highest_degree="MBA",
            school="MIT",
            graduation_year="2026",
            field_of_study="Business"
        ),
        screening=ScreeningInfo(
            years_experience="5",
            willing_to_relocate=True,
            desired_salary="200000",
            how_did_you_hear="Indeed"
        ),
        source=SourceTracking(default="Indeed")
    )

@pytest.fixture
def mock_listing():
    listing = MagicMock(spec=JobListing)
    listing.title = "Software Engineer"
    listing.company = "TechCorp"
    listing.url = "https://example.com/apply"
    listing.dedup_key = "test_key"
    return listing

def test_build_agent_system_prompt(mock_profile, mock_listing):
    resume_path = Path("/tmp/resume.pdf")
    cover_letter_path = Path("/tmp/cl.pdf")
    prompt = _build_agent_system_prompt(
        mock_profile, mock_listing, resume_path, cover_letter_path, ats_platform="workday"
    )
    
    assert "Sam Kokomoor" in prompt
    assert "Software Engineer" in prompt
    assert "TechCorp" in prompt
    assert "/tmp/resume.pdf" in prompt
    assert "/tmp/cl.pdf" in prompt
    assert "Workday-specific guidance" in prompt

@pytest.mark.asyncio
async def test_fill_application_with_agent_success(mock_profile, mock_listing):
    mock_page = AsyncMock()
    mock_llm = MagicMock()
    
    # Mock controller and its run method
    with MagicMock() as mock_controller_cls:
        from pipelines.job_agent.application.agent_filler import WebAgentController
        import pipelines.job_agent.application.agent_filler as agent_filler
        
        mock_controller = AsyncMock()
        mock_controller.run.return_value = AgentResult(
            status="completed",
            steps_taken=5,
            final_url="https://example.com/done",
            summary="Application submitted successfully"
        )
        
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("pipelines.job_agent.application.agent_filler.WebAgentController", lambda **kwargs: mock_controller)
            
            result = await fill_application_with_agent(
                mock_listing,
                mock_profile,
                Path("/tmp/resume.pdf"),
                None,
                page=mock_page,
                llm=mock_llm,
                run_id="test_run"
            )
            
            assert isinstance(result, ApplicationAttempt)
            assert result.status == "submitted"
            assert result.steps_taken == 5
            assert "Application submitted successfully" in result.summary
