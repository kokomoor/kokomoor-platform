"""Site-specific scraper wrappers.

Each wrapper adapts the generic scraping engine to a specific site's
navigation, authentication, and extraction requirements. Most sites
can use ``BaseSiteWrapper`` with only a ``SiteProfile``; override
individual methods for site-specific quirks.
"""
