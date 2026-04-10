"""Structured page-state extraction for LLM consumption.

Extracts a token-efficient representation of the current page that gives
an LLM enough context to decide what to do next without sending raw HTML.

The ``PageState`` output is designed to serialize to ~500-1000 tokens,
making it far cheaper than vision-based approaches.

The ``index`` on each ``ElementInfo`` is the key design choice: the LLM
refers to elements by index (``"click element [3]"``) and the observer
maintains a stable mapping from index to CSS selector for that snapshot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from playwright.async_api import ElementHandle, Page

logger = structlog.get_logger(__name__)

_TAG_TO_ROLE: dict[str, str] = {
    "a": "link",
    "button": "button",
    "input": "textbox",
    "select": "combobox",
    "textarea": "textbox",
    "checkbox": "checkbox",
    "radio": "radio",
}

_PROGRESS_RE = re.compile(r"(?:step|page|question)\s*(\d+)\s*(?:of|/)\s*(\d+)", re.IGNORECASE)

_ERROR_SELECTORS = [
    ".error, .error-message, .field-error, .form-error",
    "[role='alert']",
    ".invalid-feedback",
    ".validation-error, .validation-message",
]


@dataclass(frozen=True)
class ElementInfo:
    """One interactive element on the page."""

    index: int
    tag: str
    role: str
    label: str
    selector: str
    value: str = ""
    element_type: str = ""
    options: list[str] = field(default_factory=list)
    required: bool = False
    disabled: bool = False

    def to_prompt_line(self) -> str:
        """One-line summary for inclusion in an LLM prompt."""
        parts = [f"[{self.index}]", self.role]
        if self.label:
            parts.append(f'"{self.label}"')
        if self.element_type and self.element_type not in ("submit", "button"):
            parts.append(f"type={self.element_type}")
        if self.value:
            parts.append(f'value="{self.value[:60]}"')
        if self.options:
            opts = ", ".join(self.options[:8])
            if len(self.options) > 8:
                opts += f" (+{len(self.options) - 8})"
            parts.append(f"options=[{opts}]")
        if self.required:
            parts.append("required")
        if self.disabled:
            parts.append("disabled")
        return " ".join(parts)


@dataclass(frozen=True)
class FormInfo:
    """A ``<form>`` with its child fields."""

    action: str
    method: str
    fields: list[ElementInfo] = field(default_factory=list)


@dataclass(frozen=True)
class PageState:
    """Token-efficient snapshot of the current page for LLM consumption."""

    url: str
    title: str
    visible_text: str
    forms: list[FormInfo] = field(default_factory=list)
    interactive_elements: list[ElementInfo] = field(default_factory=list)
    error_messages: list[str] = field(default_factory=list)
    progress_indicator: str = ""

    def to_prompt(self) -> str:
        """Serialize to a compact text block for inclusion in an LLM prompt."""
        lines: list[str] = [
            f"URL: {self.url}",
            f"Title: {self.title}",
        ]
        if self.progress_indicator:
            lines.append(f"Progress: {self.progress_indicator}")
        if self.error_messages:
            lines.append("Errors: " + "; ".join(self.error_messages))
        if self.visible_text:
            lines.append(f"Page text: {self.visible_text}")

        if self.forms:
            for fi, form in enumerate(self.forms):
                lines.append(f"Form {fi} ({form.method.upper()} {form.action}):")
                for el in form.fields:
                    lines.append(f"  {el.to_prompt_line()}")

        if self.interactive_elements:
            lines.append("Interactive elements:")
            for el in self.interactive_elements:
                lines.append(f"  {el.to_prompt_line()}")

        return "\n".join(lines)


# Index → ElementHandle mapping kept per snapshot
_IndexMap = dict[int, "ElementHandle"]


class PageObserver:
    """Extract structured, token-efficient page state for LLM consumption."""

    def __init__(self) -> None:
        self._index_map: _IndexMap = {}
        self._next_index: int = 0

    def _assign_index(self, el: ElementHandle) -> int:
        idx = self._next_index
        self._next_index += 1
        self._index_map[idx] = el
        return idx

    def reset(self) -> None:
        """Clear index mapping (call before each new observation)."""
        self._index_map = {}
        self._next_index = 0

    async def get_element_by_index(self, index: int) -> ElementHandle | None:
        """Retrieve an element handle from the latest snapshot by index."""
        return self._index_map.get(index)

    # ------------------------------------------------------------------
    # Primary entry point
    # ------------------------------------------------------------------

    async def get_state(
        self,
        page: Page,
        *,
        max_elements: int = 50,
        max_text_chars: int = 2000,
    ) -> PageState:
        """Extract a complete ``PageState`` from the current page."""
        self.reset()

        url = page.url
        try:
            title = await page.title()
        except Exception:
            title = ""

        forms = await self._extract_forms(page, max_elements=max_elements)
        form_indices = {el.index for form in forms for el in form.fields}

        interactive = await self._extract_interactive(
            page, max_elements=max_elements, exclude_indices=form_indices
        )
        errors = await self._extract_errors(page)
        progress = await self._extract_progress(page)
        visible = await self._extract_visible_text(page, max_chars=max_text_chars)

        state = PageState(
            url=url,
            title=title,
            visible_text=visible,
            forms=forms,
            interactive_elements=interactive,
            error_messages=errors,
            progress_indicator=progress,
        )
        logger.debug(
            "page_state_extracted",
            url=url,
            forms=len(forms),
            elements=self._next_index,
            text_len=len(visible),
        )
        return state

    async def get_form_fields(self, page: Page) -> list[FormInfo]:
        """Extract only form information (lighter than full state)."""
        self.reset()
        return await self._extract_forms(page, max_elements=100)

    # ------------------------------------------------------------------
    # Internal extraction helpers
    # ------------------------------------------------------------------

    async def _extract_forms(self, page: Page, *, max_elements: int) -> list[FormInfo]:
        forms: list[FormInfo] = []
        try:
            form_handles = await page.query_selector_all("form")
        except Exception:
            return forms

        for form_el in form_handles:
            if self._next_index >= max_elements:
                break
            try:
                action: str = await form_el.get_attribute("action") or ""
                method: str = await form_el.get_attribute("method") or "get"
            except Exception:
                action, method = "", "get"

            fields = await self._extract_fields_in(form_el, page, max_elements)
            forms.append(FormInfo(action=action, method=method, fields=fields))
        return forms

    async def _extract_fields_in(
        self,
        container: ElementHandle,
        page: Page,
        max_elements: int,
    ) -> list[ElementInfo]:
        fields: list[ElementInfo] = []
        try:
            inputs = await container.query_selector_all(
                "input, select, textarea, button[type='submit'], button:not([type])"
            )
        except Exception:
            return fields

        for el in inputs:
            if self._next_index >= max_elements:
                break
            info = await self._element_to_info(el, page)
            if info is not None:
                fields.append(info)
        return fields

    async def _extract_interactive(
        self,
        page: Page,
        *,
        max_elements: int,
        exclude_indices: set[int],
    ) -> list[ElementInfo]:
        elements: list[ElementInfo] = []
        try:
            handles = await page.query_selector_all(
                "a[href], button, [role='button'], [role='tab'], [role='link'], [onclick]"
            )
        except Exception:
            return elements

        for el in handles:
            if self._next_index >= max_elements:
                break
            info = await self._element_to_info(el, page)
            if info is not None and info.index not in exclude_indices:
                elements.append(info)
        return elements

    async def _element_to_info(self, el: ElementHandle, page: Page) -> ElementInfo | None:
        try:
            props: dict[str, Any] = await page.evaluate(
                """(el) => {
                    const tag = el.tagName.toLowerCase();
                    const type = el.type || '';
                    if (tag === 'input' && type === 'hidden') return null;
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 && rect.height === 0) return null;
                    const labels = el.labels ? Array.from(el.labels).map(l => l.textContent.trim()).filter(Boolean) : [];
                    const ariaLabel = el.getAttribute('aria-label') || '';
                    const placeholder = el.getAttribute('placeholder') || '';
                    const name = el.getAttribute('name') || '';
                    const id = el.id || '';
                    const label = labels[0] || ariaLabel || placeholder || name || el.textContent?.trim().substring(0, 80) || '';
                    let selector = '';
                    if (id) selector = '#' + CSS.escape(id);
                    else if (name) selector = tag + '[name="' + name + '"]';
                    else selector = '';
                    const value = el.value || '';
                    const required = el.required || el.getAttribute('aria-required') === 'true';
                    const disabled = el.disabled || el.getAttribute('aria-disabled') === 'true';
                    let options = [];
                    if (tag === 'select') {
                        options = Array.from(el.options).map(o => o.textContent.trim()).slice(0, 20);
                    }
                    const role = el.getAttribute('role') || '';
                    return { tag, type, label, selector, value, required, disabled, options, role };
                }""",
                el,
            )
        except Exception:
            return None

        if props is None:
            return None

        tag: str = props.get("tag", "")
        etype: str = props.get("type", "")
        role_attr: str = props.get("role", "")

        if etype in ("checkbox", "radio"):
            role = etype
        elif role_attr:
            role = role_attr
        else:
            role = _TAG_TO_ROLE.get(tag, tag)

        idx = self._assign_index(el)
        return ElementInfo(
            index=idx,
            tag=tag,
            role=role,
            label=str(props.get("label", ""))[:120],
            selector=str(props.get("selector", "")),
            value=str(props.get("value", ""))[:200],
            element_type=etype,
            options=[str(o) for o in props.get("options", [])],
            required=bool(props.get("required", False)),
            disabled=bool(props.get("disabled", False)),
        )

    async def _extract_errors(self, page: Page) -> list[str]:
        errors: list[str] = []
        combined = ", ".join(_ERROR_SELECTORS)
        try:
            handles = await page.query_selector_all(combined)
            for el in handles[:5]:
                text: str | None = await el.text_content()
                if text and text.strip():
                    errors.append(text.strip()[:200])
        except Exception:
            pass
        return errors

    async def _extract_progress(self, page: Page) -> str:
        try:
            body_text: str = await page.evaluate(
                "() => document.body?.innerText?.substring(0, 3000) || ''"
            )
            match = _PROGRESS_RE.search(body_text)
            if match:
                return f"Step {match.group(1)} of {match.group(2)}"
        except Exception:
            pass

        try:
            progress_el = await page.query_selector(
                "[role='progressbar'], .progress-bar, .stepper, .wizard-step"
            )
            if progress_el:
                aria_val = await progress_el.get_attribute("aria-valuenow")
                aria_max = await progress_el.get_attribute("aria-valuemax")
                if aria_val and aria_max:
                    return f"{aria_val}/{aria_max}"
                text = await progress_el.text_content()
                if text and text.strip():
                    return text.strip()[:80]
        except Exception:
            pass

        return ""

    async def _extract_visible_text(self, page: Page, *, max_chars: int) -> str:
        try:
            raw: str = await page.evaluate(
                """() => {
                    const sel = 'main, [role="main"], article, .content, #content';
                    const main = document.querySelector(sel);
                    const root = main || document.body;
                    if (!root) return '';
                    const walker = document.createTreeWalker(
                        root, NodeFilter.SHOW_TEXT, null
                    );
                    const parts = [];
                    let total = 0;
                    while (walker.nextNode()) {
                        const t = walker.currentNode.textContent.trim();
                        if (t.length > 1) {
                            parts.push(t);
                            total += t.length;
                            if (total > 5000) break;
                        }
                    }
                    return parts.join(' ');
                }"""
            )
            text = " ".join(raw.split())
            return text[:max_chars]
        except Exception:
            return ""
