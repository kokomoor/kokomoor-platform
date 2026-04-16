"""Tests for the LinkedIn Easy Apply template, especially button detection.

Regression guard for the stale-selector bug that blocked 100% of application
attempts: the screenshot showed a visible Easy Apply button while every
class-based selector returned None. These tests cover the aria-label-first
detection and the Easy-Apply-vs-external-redirect classification.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pipelines.job_agent.application.templates.linkedin_easy_apply import (
    _APPLY_BUTTON_SELECTORS,
    _EASY_APPLY_BUTTON_SELECTORS,
    _dismiss_blocker_modals,
    _locate_apply_button,
)


def _make_button(*, aria_label: str = "", text: str = "", visible: bool = True) -> MagicMock:
    btn = MagicMock()
    btn.get_attribute = AsyncMock(return_value=aria_label or None)
    btn.text_content = AsyncMock(return_value=text)
    btn.is_visible = AsyncMock(return_value=visible)
    return btn


def _make_page_with_selectors(selector_to_button: dict[str, MagicMock | None]) -> MagicMock:
    """Build a fake Playwright page whose query_selector returns our fakes."""
    page = MagicMock()

    async def _query(sel: str) -> MagicMock | None:
        return selector_to_button.get(sel)

    page.query_selector = AsyncMock(side_effect=_query)
    return page


class TestLocateApplyButton:
    @pytest.mark.asyncio
    async def test_finds_easy_apply_via_aria_label(self) -> None:
        btn = _make_button(
            aria_label="Easy Apply to Senior Product Manager at Meet Life Sciences",
            text="Easy Apply",
        )
        page = _make_page_with_selectors({
            "button[aria-label^='Easy Apply']": btn,
        })

        element, is_easy_apply = await _locate_apply_button(page)

        assert element is btn
        assert is_easy_apply is True

    @pytest.mark.asyncio
    async def test_classifies_external_redirect_button(self) -> None:
        """Stripe's LinkedIn listing exposes a plain 'Apply' button that
        redirects to Greenhouse — must be classified as non-Easy-Apply."""
        btn = _make_button(
            aria_label="Apply to Engineering Manager on company website",
            text="Apply",
        )
        page = _make_page_with_selectors({
            "button[aria-label^='Apply on']": btn,
        })

        element, is_easy_apply = await _locate_apply_button(page)

        assert element is btn
        assert is_easy_apply is False

    @pytest.mark.asyncio
    async def test_returns_none_when_no_button(self) -> None:
        page = _make_page_with_selectors({})

        element, is_easy_apply = await _locate_apply_button(page)

        assert element is None
        assert is_easy_apply is False

    @pytest.mark.asyncio
    async def test_skips_invisible_buttons(self) -> None:
        invisible = _make_button(
            aria_label="Easy Apply to Dev",
            visible=False,
        )
        page = _make_page_with_selectors({
            "button[aria-label^='Easy Apply']": invisible,
        })

        element, is_easy_apply = await _locate_apply_button(page)

        assert element is None
        assert is_easy_apply is False

    @pytest.mark.asyncio
    async def test_class_fallback_distinguishes_easy_vs_plain(self) -> None:
        """When only the legacy .jobs-apply-button class matches (no aria
        starts-with hit), we still need to know whether it's Easy Apply."""
        easy = _make_button(
            aria_label="",
            text="Easy Apply",
        )
        page = _make_page_with_selectors({
            "button[aria-label^='Easy Apply']": None,
            ".jobs-apply-button": easy,
        })

        element, is_easy_apply = await _locate_apply_button(page)

        assert element is easy
        assert is_easy_apply is True

    @pytest.mark.asyncio
    async def test_class_fallback_classifies_plain_apply(self) -> None:
        plain = _make_button(aria_label="", text="Apply")
        page = _make_page_with_selectors({
            "button[aria-label^='Easy Apply']": None,
            ".jobs-apply-button": plain,
        })

        element, is_easy_apply = await _locate_apply_button(page)

        assert element is plain
        assert is_easy_apply is False

    @pytest.mark.asyncio
    async def test_anchor_easy_apply_is_found(self) -> None:
        """LinkedIn may render Easy Apply as an <a> tag. The new
        a[aria-label^='Easy Apply'] selector must catch it."""
        anchor = _make_button(
            aria_label="Easy Apply to Staff Engineer at Anthropic",
            text="Easy Apply",
        )
        page = _make_page_with_selectors({
            "button[aria-label^='Easy Apply']": None,  # button variant absent
            "a[aria-label^='Easy Apply']": anchor,
        })

        element, is_easy_apply = await _locate_apply_button(page)

        assert element is anchor
        assert is_easy_apply is True

    @pytest.mark.asyncio
    async def test_anchor_external_apply_is_found(self) -> None:
        """LinkedIn external 'Apply' rendered as <a> (external link arrow).
        Regression: the OpenLoop listing (5a36c4f1) returned 'Easy Apply button
        not found' because all button-based selectors missed the <a> element."""
        anchor = _make_button(
            aria_label="Apply to Senior Software Engineer at OpenLoop on company website",
            text="Apply",
        )
        page = _make_page_with_selectors({
            "button[aria-label^='Easy Apply']": None,
            "a[aria-label^='Easy Apply']": None,
            ".jobs-apply-button": None,
            "button[aria-label^='Apply to']": None,
            "a[aria-label^='Apply to']": anchor,
        })

        element, is_easy_apply = await _locate_apply_button(page)

        assert element is anchor
        assert is_easy_apply is False

    @pytest.mark.asyncio
    async def test_broad_fallback_finds_apply_anchor(self) -> None:
        """When all named selectors miss, the broad candidate scan must find a
        visible anchor with aria-label 'apply to ...' and classify it False."""
        anchor = _make_button(
            aria_label="Apply to SWE at Obscure Co on company website",
            text="Apply",
        )
        # _make_page_with_selectors only stubs query_selector; we need
        # query_selector_all for the broad fallback. Build a custom page mock.
        page = MagicMock()
        page.query_selector = AsyncMock(return_value=None)  # all named selectors miss
        page.query_selector_all = AsyncMock(return_value=[anchor])

        element, is_easy_apply = await _locate_apply_button(page)

        assert element is anchor
        assert is_easy_apply is False


class TestDismissBlockerModals:
    """Regression guard for the 2026-04-15 'Sign in to see who you know'
    overlay that blocked Apply button detection even with a valid session."""

    @pytest.mark.asyncio
    async def test_dismisses_visible_artdeco_modal(self) -> None:
        """When an artdeco modal with a Dismiss button is present, it is
        clicked and the helper returns True."""
        from core.browser.human_behavior import HumanBehavior

        dismiss_btn = MagicMock()
        dismiss_btn.is_visible = AsyncMock(return_value=True)
        dismiss_btn.bounding_box = AsyncMock(return_value={"x": 100, "y": 100, "width": 20, "height": 20})

        modal = MagicMock()
        modal.is_visible = AsyncMock(return_value=True)

        async def _modal_query(sel: str) -> MagicMock | None:
            if "Dismiss" in sel or "dismiss" in sel:
                return dismiss_btn
            return None

        modal.query_selector = AsyncMock(side_effect=_modal_query)

        page = MagicMock()

        async def _page_query(sel: str) -> MagicMock | None:
            if "artdeco-modal" in sel or "dialog" in sel:
                return modal
            return None

        page.query_selector = AsyncMock(side_effect=_page_query)
        page.mouse = AsyncMock()
        page.mouse.click = AsyncMock()

        behavior = HumanBehavior()
        dismissed = await _dismiss_blocker_modals(page, behavior)

        assert dismissed is True
        dismiss_btn.bounding_box.assert_awaited()

    @pytest.mark.asyncio
    async def test_returns_false_when_no_modal_present(self) -> None:
        """When no modal is on the page, the helper returns False without error."""
        from core.browser.human_behavior import HumanBehavior

        page = MagicMock()
        page.query_selector = AsyncMock(return_value=None)

        behavior = HumanBehavior()
        dismissed = await _dismiss_blocker_modals(page, behavior)

        assert dismissed is False

    @pytest.mark.asyncio
    async def test_skips_invisible_modal(self) -> None:
        """A modal present in DOM but not visible (display:none) is not dismissed."""
        from core.browser.human_behavior import HumanBehavior

        modal = MagicMock()
        modal.is_visible = AsyncMock(return_value=False)

        page = MagicMock()
        page.query_selector = AsyncMock(return_value=modal)

        behavior = HumanBehavior()
        dismissed = await _dismiss_blocker_modals(page, behavior)

        assert dismissed is False


class TestSelectorsShape:
    """Static sanity checks — prevent accidental removal of critical selectors."""

    def test_easy_apply_primary_selector_is_aria_label(self) -> None:
        assert _EASY_APPLY_BUTTON_SELECTORS[0].startswith("button[aria-label")
        assert "Easy Apply" in _EASY_APPLY_BUTTON_SELECTORS[0]

    def test_apply_selectors_are_anchored(self) -> None:
        """`^=` prevents matching 'Apply filters' and other false positives.

        aria-label-based selectors must be exact or starts-with anchored.
        Class/attribute-based fallbacks (no aria-label) are also allowed as
        long as they're narrowly scoped (e.g. a.jobs-apply-button).
        """
        for sel in _APPLY_BUTTON_SELECTORS:
            if "aria-label" in sel:
                # aria-label selectors must be anchored
                assert "='Apply'" in sel or "^='Apply" in sel
            else:
                # Non-aria-label selectors must be narrowly scoped
                # (class or attribute-based, not bare element selectors)
                assert "." in sel or "[" in sel, (
                    f"Non-aria-label selector must be class/attr-scoped: {sel!r}"
                )
