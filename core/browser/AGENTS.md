# AGENTS.md — core/browser/

Playwright lifecycle management, anti-detection, and session persistence.

## Stealth stack

Two layers of anti-detection, applied automatically when `KP_ENABLE_BROWSER_STEALTH=true`:

| Layer | Applied by | Covers |
|-------|-----------|--------|
| **Context-level** (`apply_stealth_defaults()`) | `BrowserManager.__aenter__` | User-agent rotation, viewport/screen size, timezone, locale, color scheme, device scale factor |
| **Page-level** (`ANTI_DETECTION_SCRIPT`) | `BrowserManager.new_page` via `apply_page_stealth()` | `navigator.webdriver` flag, `navigator.plugins` spoofing, WebGL vendor/renderer, canvas fingerprint noise |

Context-level settings are Playwright context options. Page-level patches are injected as an init script that runs before any page JS (including bot-detection libraries).

## Session persistence

`BrowserManager` accepts an optional `storage_state` keyword argument:

```python
# First run — no prior session
async with BrowserManager() as browser:
    page = await browser.new_page()
    # ... interact, log in, etc.
    state = await browser.dump_storage_state()
    # persist `state` (dict) to disk / database

# Next run — restore session
async with BrowserManager(storage_state=state) as browser:
    page = await browser.new_page()
    # cookies and storage are pre-loaded
```

- `storage_state` is a `dict[str, Any]` (Playwright's storage state format: cookies + origins with localStorage).
- Pass `None` (the default) to start a fresh session — the kwarg is omitted from `new_context()` entirely.
- `dump_storage_state()` returns the current context's full storage snapshot. Call it before exiting the context manager.
- **Intended pattern:** dump after each provider run, persist to disk or DB, restore on next run via the `storage_state=` param.

## Rules

- **All browser interactions must go through `BrowserManager`.** Never use raw Playwright `browser`, `context`, or `page` objects outside of it. This ensures stealth, rate limiting, and resource cleanup are always applied.
- **`new_page()` automatically applies page-level stealth** when `KP_ENABLE_BROWSER_STEALTH=true`. Do not call `apply_page_stealth()` manually on pages obtained from `BrowserManager`.
- **Do not pass `storage_state=None` to Playwright** — it causes errors on some versions. `BrowserManager` handles this by omitting the kwarg when the value is `None`.
- **Do not add pipeline-specific logic here.** Job-board selectors, login flows, and extraction heuristics belong in the pipeline, not in `core/browser/`.
