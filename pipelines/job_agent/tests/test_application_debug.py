import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pipelines.job_agent.application._debug import capture_application_failure
from pipelines.job_agent.models import JobListing

@pytest.mark.asyncio
async def test_capture_application_failure():
    mock_page = AsyncMock()
    mock_page.url = "https://example.com/fail"
    mock_page.title.return_value = "Failure Page"
    
    mock_listing = MagicMock(spec=JobListing)
    mock_listing.dedup_key = "test_dedup_key"
    
    # Mock settings to enable capture
    with patch("pipelines.job_agent.application._debug.get_settings") as mock_get_settings:
        mock_settings = MagicMock()
        mock_settings.application_debug_capture_enabled = True
        mock_settings.application_debug_capture_dir = "/tmp/app_debug"
        mock_settings.application_debug_capture_html = True
        mock_get_settings.return_value = mock_settings
        
        # Mock FailureCapture.capture_page_failure to return a fake artifact list
        with patch("pipelines.job_agent.application._debug.FailureCapture.capture_page_failure") as mock_capture:
            mock_capture.return_value = ["/tmp/app_debug/run1/test_dedup_key/event/page.png", "/tmp/app_debug/run1/test_dedup_key/event/page.html"]
            
            path = await capture_application_failure(
                mock_page, mock_listing, "run1", "test_stage", "test_reason"
            )
            
            assert "page.png" in path
            mock_capture.assert_called_once()
            args, kwargs = mock_capture.call_args
            assert kwargs["source"] == "test_dedup_key"
            assert kwargs["stage"] == "test_stage"
            assert kwargs["reason"] == "test_reason"
