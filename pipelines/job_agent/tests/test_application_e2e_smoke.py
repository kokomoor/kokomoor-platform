import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pipelines.job_agent.application.node import application_node
from pipelines.job_agent.state import JobAgentState
from pipelines.job_agent.models import JobListing, ApplicationAttempt

@pytest.mark.asyncio
async def test_application_node_smoke():
    # Setup state with one tailored listing
    listing = JobListing(
        title="Software Engineer",
        company="TechCorp",
        location="Remote",
        url="https://boards.greenhouse.io/acme/jobs/123",
        dedup_key="smoke_test_key"
    )
    
    state = JobAgentState(
        run_id="smoke_run",
        tailored_listings=[listing],
        dry_run=True
    )
    
    # Mock route_application and the greenhouse submitter
    with patch("pipelines.job_agent.application.node.route_application") as mock_route:
        from pipelines.job_agent.application.router import RouteDecision, SubmissionStrategy
        mock_route.return_value = RouteDecision(
            strategy=SubmissionStrategy.API_GREENHOUSE,
            application_url=listing.url,
            ats_platform="greenhouse",
            requires_browser=False,
            requires_account=False
        )
        
        with patch("pipelines.job_agent.application.node.get_submitter") as mock_get_submitter:
            mock_submitter = AsyncMock()
            mock_submitter.return_value = ApplicationAttempt(
                dedup_key=listing.dedup_key,
                status="submitted",
                strategy="api_greenhouse",
                summary="Smoke test success"
            )
            mock_get_submitter.return_value = mock_submitter
            
            # Mock dedup store to avoid DB issues
            with patch("pipelines.job_agent.application.dedup.ApplicationDedupStore") as mock_dedup_cls:
                mock_dedup = MagicMock()
                mock_dedup.filter_unapplied = AsyncMock(side_effect=lambda x: x)
                mock_dedup.mark_applied = AsyncMock()
                mock_dedup_cls.return_value = mock_dedup
                
                # Run the node
                new_state = await application_node(state)
                
                # Verify
                assert len(new_state.application_results) == 1
                assert new_state.application_results[0].status == "submitted"
                assert "Smoke test success" in new_state.application_results[0].summary
                
                # Check that dedup store was used
                mock_dedup.mark_applied.assert_called_once()
                mock_dedup.close.assert_called_once()
