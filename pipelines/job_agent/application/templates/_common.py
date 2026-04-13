"""Common utilities for browser-based application templates."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from playwright.async_api import ElementHandle, Page

    from core.browser.human_behavior import HumanBehavior

logger = structlog.get_logger(__name__)


async def get_field_label(field_el: ElementHandle, page: Page) -> str:
    """Attempt to find the label for a form field."""
    # 1. Look for <label for="...">
    field_id = await field_el.get_attribute("id")
    if field_id:
        label_el = await page.query_selector(f"label[for='{field_id}']")
        if label_el:
            return (await label_el.text_content() or "").strip()

    # 2. Look for aria-label
    aria_label = await field_el.get_attribute("aria-label")
    if aria_label:
        return aria_label.strip()

    # 3. Look for parent label
    try:
        label_text = await field_el.evaluate(
            "el => {"
            "  const label = el.closest('label') || document.querySelector(`label[for='${el.id}']`);"
            "  if (label) return label.innerText;"
            "  const group = el.closest('.fb-dash-form-element, .form-group, .field');"
            "  if (group) {"
            "    const labelEl = group.querySelector('label, .label, span[aria-hidden=\"true\"]');"
            "    if (labelEl) return labelEl.innerText;"
            "  }"
            "  return '';"
            "}"
        )
        if label_text:
            return label_text.strip()
    except Exception:
        pass

    return ""


async def select_option_fuzzy(
    field_el: ElementHandle, value: str, behavior: HumanBehavior
) -> None:
    """Select an option from a <select> or radio group using fuzzy matching."""
    try:
        # Playwright's select_option doesn't have a direct 'fuzzy' label match
        # in the way I wrote in the skeleton, but we can do it manually.
        options = await field_el.evaluate(
            "el => Array.from(el.options).map(o => ({text: o.text, value: o.value}))"
        )
        target = value.lower()
        best_match = None
        for opt in options:
            if target in opt["text"].lower() or opt["text"].lower() in target:
                best_match = opt["value"]
                break

        if best_match:
            await field_el.select_option(value=best_match)
        else:
            await field_el.select_option(label=value)
    except Exception:
        try:
            await field_el.select_option(value=value)
        except Exception:
            logger.warning("template_select_failed", value=value)
