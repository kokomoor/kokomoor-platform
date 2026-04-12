"""Provider adapters for job board scraping.

Browser providers: LinkedIn, Indeed, Built In, Wellfound, Workday, DirectSite
HTTP providers: Greenhouse, Lever (public JSON APIs — no browser required)

All browser providers extend BaseProvider and implement the ProviderAdapter protocol.
HTTP providers implement the protocol directly (no BaseProvider needed — they don't
need the browser lifecycle).
"""

from pipelines.job_agent.discovery.providers.protocol import ProviderAdapter

__all__ = ["ProviderAdapter"]
