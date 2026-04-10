# pipelines/scraper/ — Universal Scraper Pipeline

Self-contained pipeline for scraping any authenticated site with
automated validation and self-healing.

## Structure

```
pipelines/scraper/
├── models.py         All Pydantic models (SiteProfile, OutputContract, ScrapeResult, etc.)
├── wrappers/
│   ├── base.py       Generic profile-driven BaseSiteWrapper
│   ├── linkedin.py   LinkedIn-specific (Pass 3)
│   ├── indeed.py     Indeed-specific (Pass 3)
│   ├── vision_gsi.py Vision Government Solutions (Pass 2)
│   └── uslandrecords.py US Land Records (Pass 2)
├── nodes/
│   ├── scrape.py     Core scrape execution (HTTP first → browser fallback)
│   ├── validate.py   Schema + coverage + dedup + drift + freshness
│   ├── onboard.py    LLM-driven site profiling (Pass 3)
│   └── heal.py       Human-gated diagnosis + signed remediation trigger
├── tests/            Offline fixtures + scale + contract tests
├── profiles/         Site profile YAML files (committed)
└── fixtures/         Captured site snapshots (gitignored)
```

## Rules

- Imports only from `core/` — never from `pipelines.job_agent`.
- Every wrapper must work in offline mode (`extract_from_fixture(html)`).
- All extraction must be testable against saved HTML fixtures.
- Site-specific behavior goes in wrapper subclasses, not in `core/`.
- Profiles define the site; wrappers implement the quirks.
- Never hard-code credentials — use `KP_*` env var references in `AuthConfig`.
