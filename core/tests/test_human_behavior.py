"""Tests for HumanBehavior — guard against regressions in the mouse-state init bug.

The first call to human_click/move_mouse_naturally before any prior call
previously raised AttributeError because _last_x/_last_y were never
initialised in __init__. This happened in production when LinkedIn triggered
re-authentication (which exercises the login-flow click path) instead of
resuming a cached session.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.browser.human_behavior import HumanBehavior


class TestHumanBehaviorInit:
    def test_last_position_initialised_on_construction(self) -> None:
        """_last_x/_last_y must exist before any method is called."""
        behavior = HumanBehavior()
        assert hasattr(behavior, "_last_x"), "_last_x must be set by __init__"
        assert hasattr(behavior, "_last_y"), "_last_y must be set by __init__"
        assert isinstance(behavior._last_x, float)
        assert isinstance(behavior._last_y, float)

    def test_multiple_instances_have_independent_state(self) -> None:
        b1 = HumanBehavior()
        b2 = HumanBehavior()
        # Both must have the attribute; values may differ (random seed)
        assert hasattr(b1, "_last_x")
        assert hasattr(b2, "_last_x")


class TestMoveMouseNaturally:
    @pytest.mark.asyncio
    async def test_first_call_does_not_raise(self) -> None:
        """move_mouse_naturally on a fresh instance must not raise AttributeError."""
        behavior = HumanBehavior()
        page = MagicMock()
        page.mouse = MagicMock()
        page.mouse.move = AsyncMock()

        # Should not raise regardless of prior call history
        await behavior.move_mouse_naturally(page, 500.0, 300.0)

        assert behavior._last_x == pytest.approx(500.0)
        assert behavior._last_y == pytest.approx(300.0)

    @pytest.mark.asyncio
    async def test_position_updated_after_move(self) -> None:
        behavior = HumanBehavior()
        page = MagicMock()
        page.mouse = MagicMock()
        page.mouse.move = AsyncMock()

        await behavior.move_mouse_naturally(page, 100.0, 200.0)
        assert behavior._last_x == pytest.approx(100.0)
        assert behavior._last_y == pytest.approx(200.0)

        await behavior.move_mouse_naturally(page, 800.0, 600.0)
        assert behavior._last_x == pytest.approx(800.0)
        assert behavior._last_y == pytest.approx(600.0)


class TestHumanClick:
    @pytest.mark.asyncio
    async def test_click_on_fresh_instance_does_not_raise(self) -> None:
        """human_click on first use (no prior move_mouse call) must not raise."""
        behavior = HumanBehavior()
        page = MagicMock()
        page.mouse = MagicMock()
        page.mouse.move = AsyncMock()
        page.mouse.click = AsyncMock()

        element = MagicMock()
        element.bounding_box = AsyncMock(return_value={
            "x": 100.0, "y": 200.0, "width": 80.0, "height": 30.0
        })

        with patch("random.random", return_value=0.5):
            await behavior.human_click(page, element)
