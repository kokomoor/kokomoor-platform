import pytest
import os
from pathlib import Path
from pipelines.job_agent.application.dedup import ApplicationDedupStore
from pipelines.job_agent.models import JobListing

@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "test_app_dedup.db"
    return db_path

@pytest.mark.asyncio
async def test_application_dedup_store(temp_db):
    store = ApplicationDedupStore(db_path=temp_db)
    
    listing1 = JobListing(
        title="Software Engineer",
        company="TechCorp",
        location="Boston",
        url="https://example.com/1",
        dedup_key="key1"
    )
    listing2 = JobListing(
        title="Data Scientist",
        company="DataInc",
        location="Remote",
        url="https://example.com/2",
        dedup_key="key2"
    )
    
    # Initially not applied
    assert await store.is_applied(listing1) is False
    assert await store.is_applied(listing2) is False
    
    # Mark listing1 applied
    await store.mark_applied(listing1)
    assert await store.is_applied(listing1) is True
    assert await store.is_applied(listing2) is False
    
    # Filter
    unapplied = await store.filter_unapplied([listing1, listing2])
    assert len(unapplied) == 1
    assert unapplied[0].dedup_key == "key2"
    
    store.close()
