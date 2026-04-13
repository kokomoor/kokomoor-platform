import pytest
from unittest.mock import AsyncMock, MagicMock
from core.browser.actions import BrowserActions, ActionResult, NavigationResult
from core.browser.human_behavior import HumanBehavior

@pytest.fixture
def mock_page():
    page = AsyncMock()
    page.url = "http://example.com"
    return page

@pytest.fixture
def browser_actions(mock_page):
    behavior = MagicMock(spec=HumanBehavior)
    behavior.between_actions_pause = AsyncMock()
    behavior.human_click = AsyncMock()
    behavior.type_with_cadence = AsyncMock()
    return BrowserActions(page=mock_page, behavior=behavior)

@pytest.mark.asyncio
async def test_goto_success(browser_actions, mock_page):
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_page.goto.return_value = mock_resp
    
    result = await browser_actions.goto("http://example.com")
    
    assert result.success is True
    assert result.status == 200
    assert result.url == "http://example.com"
    mock_page.goto.assert_called_once_with("http://example.com", wait_until="domcontentloaded", timeout=30000)
    browser_actions._behavior.between_actions_pause.assert_called_once()

@pytest.mark.asyncio
async def test_goto_failure(browser_actions, mock_page):
    mock_page.goto.side_effect = Exception("Network error")
    
    result = await browser_actions.goto("http://example.com")
    
    assert result.success is False
    assert "Network error" in result.error
    assert result.url == "http://example.com"

@pytest.mark.asyncio
async def test_click_success(browser_actions, mock_page):
    mock_el = AsyncMock()
    mock_page.query_selector_all.return_value = [mock_el]
    
    result = await browser_actions.click("#btn")
    
    assert result.success is True
    browser_actions._behavior.human_click.assert_called_once_with(mock_page, mock_el)

@pytest.mark.asyncio
async def test_click_element_not_found(browser_actions, mock_page):
    mock_page.query_selector_all.return_value = []
    mock_page.query_selector.return_value = None
    
    result = await browser_actions.click("#btn")
    
    assert result.success is False
    assert "Element not found" in result.error
    assert result.selector_found is False

@pytest.mark.asyncio
async def test_fill_success(browser_actions, mock_page):
    mock_el = AsyncMock()
    mock_page.query_selector_all.return_value = [mock_el]
    
    result = await browser_actions.fill("#input", "test text")
    
    assert result.success is True
    browser_actions._behavior.human_click.assert_called_once_with(mock_page, mock_el)
    mock_el.evaluate.assert_called_once_with("el => el.value = ''")
    browser_actions._behavior.type_with_cadence.assert_called_once_with(mock_el, "test text")

@pytest.mark.asyncio
async def test_select_option_success(browser_actions, mock_page):
    mock_el = AsyncMock()
    mock_page.query_selector_all.return_value = [mock_el]
    
    result = await browser_actions.select_option("#select", "opt1")
    
    assert result.success is True
    mock_page.select_option.assert_called_once_with("#select", value="opt1", timeout=3000)
    
@pytest.mark.asyncio
async def test_check_success(browser_actions, mock_page):
    mock_el = AsyncMock()
    mock_el.is_checked.return_value = False
    mock_page.query_selector_all.return_value = [mock_el]
    
    result = await browser_actions.check("#checkbox")
    
    assert result.success is True
    browser_actions._behavior.human_click.assert_called_once_with(mock_page, mock_el)
