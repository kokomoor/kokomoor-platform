import pytest
from unittest.mock import AsyncMock, MagicMock
from pipelines.job_agent.application.workday_helpers import detect_workday_account_wall, verify_workday_prefill

@pytest.mark.asyncio
async def test_detect_workday_account_wall():
    mock_page = AsyncMock()
    
    # Case 1: Account wall found
    mock_page.wait_for_selector.side_effect = lambda s, **kw: MagicMock() if "signInForm" in s else None
    assert await detect_workday_account_wall(mock_page) is True
    
    # Case 2: No account wall
    mock_page.wait_for_selector.side_effect = lambda s, **kw: None
    assert await detect_workday_account_wall(mock_page) is False

@pytest.mark.asyncio
async def test_verify_workday_prefill():
    mock_page = AsyncMock()
    mock_profile = MagicMock()
    mock_profile.personal.first_name = "Sam"
    mock_profile.personal.last_name = "Kokomoor"
    mock_profile.personal.email = "sam@example.com"
    mock_profile.personal.phone_formatted = "(123) 456-7890"
    
    # Case 1: All correct
    mock_el = AsyncMock()
    mock_el.get_attribute.side_effect = lambda attr: {
        "legalNameSection_firstName": "Sam",
        "legalNameSection_lastName": "Kokomoor",
        "emailInput": "sam@example.com",
        "phoneInput": "(123) 456-7890",
    }.get(None) # Not how it works in mock side_effect, need a better one
    
    # Fix side_effect for verify_workday_prefill
    async def mock_query(selector, **kwargs):
        el = AsyncMock()
        if "firstName" in selector: el.get_attribute.return_value = "Sam"
        elif "lastName" in selector: el.get_attribute.return_value = "Kokomoor"
        elif "email" in selector: el.get_attribute.return_value = "sam@example.com"
        elif "phone" in selector: el.get_attribute.return_value = "(123) 456-7890"
        return el
        
    mock_page.wait_for_selector.side_effect = mock_query
    
    mismatches = await verify_workday_prefill(mock_page, mock_profile)
    assert len(mismatches) == 0
    
    # Case 2: Mismatch
    async def mock_query_mismatch(selector, **kwargs):
        el = AsyncMock()
        if "firstName" in selector: el.get_attribute.return_value = "WrongName"
        else: el.get_attribute.return_value = "Correct" # Placeholder for others
        return el
    
    mock_page.wait_for_selector.side_effect = mock_query_mismatch
    # We need to make sure the expected values match "Correct" for other fields to isolate firstName
    mock_profile.personal.last_name = "Correct"
    mock_profile.personal.email = "Correct"
    mock_profile.personal.phone_formatted = "Correct"
    
    mismatches = await verify_workday_prefill(mock_page, mock_profile)
    assert "firstName" in mismatches
    assert len(mismatches) == 1
    mock_profile.personal.phone_formatted = "Correct"
    
    mismatches = await verify_workday_prefill(mock_page, mock_profile)
    assert "firstName" in mismatches
    assert len(mismatches) == 1