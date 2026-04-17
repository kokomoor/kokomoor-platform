import pytest
from unittest.mock import AsyncMock, patch
from pipelines.job_agent.application.notifications import notify_application_status, notify_application_batch_summary
from pipelines.job_agent.models import JobListing, ApplicationAttempt

@pytest.mark.asyncio
async def test_notify_application_status():
    listing = JobListing(
        title="Software Engineer",
        company="TechCorp",
        location="Boston",
        url="https://example.com/1",
        dedup_key="key1"
    )
    attempt = ApplicationAttempt(
        dedup_key="key1",
        status="submitted",
        strategy="api_greenhouse",
        summary="Applied via Greenhouse API"
    )
    
    with patch("pipelines.job_agent.application.notifications.send_notification", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        
        await notify_application_status(listing, attempt)
        
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        assert "✅ Application Submitted" in kwargs["subject"]
        assert "TechCorp" in kwargs["subject"]
        assert "Applied via Greenhouse API" in kwargs["body"]

@pytest.mark.asyncio
async def test_notify_application_batch_summary():
    listing1 = JobListing(title="SE", company="C1", location="L1", url="U1", dedup_key="K1")
    attempt1 = ApplicationAttempt(dedup_key="K1", status="submitted", strategy="s1", summary="sm1")
    
    listing2 = JobListing(title="DS", company="C2", location="L2", url="U2", dedup_key="K2")
    attempt2 = ApplicationAttempt(dedup_key="K2", status="error", strategy="s2", summary="sm2")
    
    attempts = [(listing1, attempt1), (listing2, attempt2)]
    
    with patch("pipelines.job_agent.application.notifications.send_notification", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        
        await notify_application_batch_summary(attempts)
        
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        assert "1 submitted" in kwargs["subject"]
        assert "[SUBMITTED] C1: SE" in kwargs["body"]
        assert "[ERROR] C2: DS" in kwargs["body"]
