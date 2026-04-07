"""Job-page extraction helpers for manual URL workflows.

HTML transport uses ``core.fetch`` (`HttpFetcher`, `BrowserFetcher`); parsing and
normalization to ``JobListing`` stay in this package.
"""

from pipelines.job_agent.extraction.inspection import (
    write_extracted_job_markdown,
    write_job_analysis_markdown,
)
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
    "write_extracted_job_markdown",
    "write_job_analysis_markdown",
]
