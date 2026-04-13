import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from pipelines.job_agent.application.router import route_application, SubmissionStrategy
from pipelines.job_agent.models import JobListing

def _listing(url: str) -> JobListing:
    return JobListing(
        title="Software Engineer",
        company="Example Co",
        location="Remote",
        url=url,
        dedup_key=f"dedup::{url}",
    )

@pytest.mark.asyncio
async def test_route_application_linkedin_redirect_to_greenhouse():
    initial_url = "https://www.linkedin.com/jobs/view/12345/"
    final_url = "https://boards.greenhouse.io/acme/jobs/999"
    
    mock_page = AsyncMock()
    mock_page.url = initial_url
    mock_page.goto = AsyncMock()
    
    mock_btn = AsyncMock()
    mock_btn.is_visible.return_value = True
    mock_page.query_selector.return_value = mock_btn
    
    mock_new_page = AsyncMock()
    mock_new_page.url = final_url
    mock_new_page.wait_for_load_state = AsyncMock()
    
    # Value must be awaitable
    mock_new_page_info = MagicMock()
    future = asyncio.Future()
    future.set_result(mock_new_page)
    mock_new_page_info.value = future
    
    # Mocking async context manager for expect_page
    class AsyncContextManagerMock:
        async def __aenter__(self):
            return mock_new_page_info
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_page.context.expect_page = MagicMock(return_value=AsyncContextManagerMock())
    
    decision = await route_application(_listing(initial_url), page=mock_page)
    
    assert decision.strategy == SubmissionStrategy.API_GREENHOUSE
    assert decision.application_url == final_url
    assert decision.ats_platform == "greenhouse"
    assert decision.requires_browser is False

@pytest.mark.asyncio
async def test_route_application_linkedin_no_button_stays_linkedin():
    initial_url = "https://www.linkedin.com/jobs/view/12345/"
    
    mock_page = AsyncMock()
    mock_page.url = initial_url
    mock_page.goto = AsyncMock()
    mock_page.query_selector.return_value = None
    
    decision = await route_application(_listing(initial_url), page=mock_page)
    
    assert decision.strategy == SubmissionStrategy.TEMPLATE_LINKEDIN_EASY_APPLY
    assert decision.application_url == initial_url
    assert decision.ats_platform == "linkedin"

@pytest.mark.asyncio
async def test_route_application_indeed_redirect_to_lever():
    initial_url = "https://www.indeed.com/viewjob?jk=12345"
    final_url = "https://jobs.lever.co/acme/uuid"
    
    mock_page = AsyncMock()
    mock_page.url = initial_url
    mock_page.goto = AsyncMock()
    
    mock_btn = AsyncMock()
    mock_btn.is_visible.return_value = True
    mock_page.query_selector.return_value = mock_btn
    
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    # Mocking failure of expect_page
    class AsyncContextManagerFailMock:
        async def __aenter__(self):
            raise PlaywrightTimeoutError("No new page")
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_page.context.expect_page = MagicMock(return_value=AsyncContextManagerFailMock())
    
    # After click and wait, page.url changes (simulated)
    # We need to make sure _follow_apply_link returns the new url
    # In route_application: final_url = await _follow_apply_link(page)
    # Inside _follow_apply_link: 3. Wait for URL change in same tab
    async def mock_wait_for_load_state(*args, **kwargs):
        mock_page.url = final_url
    
    mock_page.wait_for_load_state = mock_wait_for_load_state
    
    decision = await route_application(_listing(initial_url), page=mock_page)
    
    assert decision.strategy == SubmissionStrategy.API_LEVER
    assert decision.application_url == final_url
    assert decision.ats_platform == "lever"
