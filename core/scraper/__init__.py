"""Core scraper infrastructure.

Shared, domain-agnostic modules for deduplication, content storage,
HTTP fetching, and fixture management.  Pipelines import from here;
site-specific logic lives in ``pipelines/scraper/wrappers/``.
"""
