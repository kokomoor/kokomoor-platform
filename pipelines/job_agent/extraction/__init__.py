"""Job-page extraction helpers for manual URL workflows."""

from pipelines.job_agent.extraction.manual_job_extractor import (
    ExtractedJobData,
    extract_job_data_from_html,
    extract_job_data_from_url,
    generate_dedup_key,
)

__all__ = [
    "ExtractedJobData",
    "extract_job_data_from_html",
    "extract_job_data_from_url",
    "generate_dedup_key",
]
