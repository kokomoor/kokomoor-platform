"""Tests for generic tailoring workflow engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import BaseModel

from core.testing import MockLLMClient
from core.workflows.tailoring import TailoringEngine, TailoringSpec


class DummyPlan(BaseModel):
    plan: str


@dataclass
class TailorState:
    items: list[str] = field(default_factory=list)
    contexts: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    skipped: bool = False
    completed: bool = False
    applied: list[tuple[str, str]] = field(default_factory=list)
    run_id: str = "rid"
    dry_run: bool = False


@dataclass(frozen=True)
class Runtime:
    out_dir: Path
    model: str | None
    max_tokens: int


def _spec(
    tmp_path: Path,
) -> TailoringSpec[TailorState, str, str, list[str], DummyPlan, str, Runtime]:
    return TailoringSpec(
        name="dummy_tailoring",
        plan_model_type=DummyPlan,
        prepare=lambda _state: Runtime(out_dir=tmp_path, model="plan-model", max_tokens=111),
        should_skip=lambda state: state.dry_run,
        on_skip=lambda state: setattr(state, "skipped", True),
        get_items=lambda state: state.items,
        load_inventory=lambda _state, _runtime: ["inventory"],
        get_context=lambda state, item, _runtime: state.contexts.get(item),
        on_missing_context=lambda state, item, _runtime: state.errors.append(f"missing:{item}"),
        on_item_start=lambda _state, _item, _runtime: None,
        build_inventory_view=lambda _state, item, context, inventory, _runtime: (
            f"{item}:{context}:{inventory[0]}"
        ),
        build_prompt=lambda _state, _item, _context, _inventory, view, _runtime: f"prompt::{view}",
        get_run_id=lambda state: state.run_id,
        get_model=lambda _state, runtime: runtime.model,
        get_max_tokens=lambda _state, runtime: runtime.max_tokens,
        validate_plan=lambda _state, _item, _context, _inventory, plan, _runtime: (
            (_ for _ in ()).throw(ValueError("invalid plan")) if plan.plan == "invalid" else None
        ),
        apply_plan=lambda state, item, _context, _inventory, plan, _runtime: _apply(
            state, item, plan.plan
        ),
        get_output_path=lambda _state, item, runtime: runtime.out_dir / f"{item}.txt",
        render_document=lambda _state, _item, document, path, _runtime: _render(path, document),
        on_item_success=lambda state, item, _plan, _doc, path, _runtime: state.outputs.__setitem__(
            item, str(path)
        ),
        on_item_error=lambda state, item, exc, _runtime: state.errors.append(f"{item}:{exc}"),
        on_complete=lambda state, _runtime: setattr(state, "completed", True),
    )


def _apply(state: TailorState, item: str, value: str) -> str:
    state.applied.append((item, value))
    return f"rendered:{item}:{value}"


@pytest.mark.asyncio
async def test_missing_context_and_skip_behavior(tmp_path: Path) -> None:
    engine: TailoringEngine[TailorState, str, str, list[str], DummyPlan, str, Runtime] = (
        TailoringEngine()
    )

    skipped_state = TailorState(items=["a"], dry_run=True)
    await engine.run(skipped_state, llm_client=MockLLMClient(responses=[]), spec=_spec(tmp_path))
    assert skipped_state.skipped is True

    state = TailorState(items=["a"], contexts={})
    await engine.run(state, llm_client=MockLLMClient(responses=[]), spec=_spec(tmp_path))
    assert state.errors == ["missing:a"]


@pytest.mark.asyncio
async def test_skip_does_not_prepare_runtime(tmp_path: Path) -> None:
    prepare_calls = 0

    def _count_prepare(_state: TailorState) -> Runtime:
        nonlocal prepare_calls
        prepare_calls += 1
        return Runtime(out_dir=tmp_path, model="plan-model", max_tokens=111)

    spec = _spec(tmp_path)
    spec = TailoringSpec(
        **{
            **spec.__dict__,
            "prepare": _count_prepare,
        }
    )

    engine: TailoringEngine[TailorState, str, str, list[str], DummyPlan, str, Runtime] = (
        TailoringEngine()
    )
    state = TailorState(items=["a"], dry_run=True)
    await engine.run(state, llm_client=MockLLMClient(responses=[]), spec=spec)

    assert prepare_calls == 0


@pytest.mark.asyncio
async def test_per_item_isolation_and_output_writeback(tmp_path: Path) -> None:
    engine: TailoringEngine[TailorState, str, str, list[str], DummyPlan, str, Runtime] = (
        TailoringEngine()
    )
    state = TailorState(items=["a", "b"], contexts={"a": "ctx-a", "b": "ctx-b"})
    client = MockLLMClient(responses=['{"plan":"ok-a"}', '{"plan":"invalid"}'])

    await engine.run(state, llm_client=client, spec=_spec(tmp_path))

    assert "a" in state.outputs
    assert Path(state.outputs["a"]).exists()
    assert any("invalid plan" in err for err in state.errors)
    assert state.completed is True


@pytest.mark.asyncio
async def test_apply_render_and_prompt_delegation(tmp_path: Path) -> None:
    engine: TailoringEngine[TailorState, str, str, list[str], DummyPlan, str, Runtime] = (
        TailoringEngine()
    )
    state = TailorState(items=["a"], contexts={"a": "ctx"})
    client = MockLLMClient(responses=['{"plan":"ok"}'])

    await engine.run(state, llm_client=client, spec=_spec(tmp_path))

    assert state.applied == [("a", "ok")]
    prompt, kwargs = client.calls[0]
    assert prompt.startswith("prompt::a:ctx:inventory")
    assert kwargs["model"] == "plan-model"
    assert kwargs["max_tokens"] == 111
    assert kwargs["run_id"] == "rid"


def _render(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


@pytest.mark.asyncio
async def test_plan_validator_triggers_retry(tmp_path: Path) -> None:
    """A ValueError from build_plan_validator must retry the LLM call."""
    engine: TailoringEngine[TailorState, str, str, list[str], DummyPlan, str, Runtime] = (
        TailoringEngine()
    )
    state = TailorState(items=["a"], contexts={"a": "ctx-a"})

    def _reject_short(_s, _i, _c, _inv, _runtime):
        def _check(plan: DummyPlan) -> None:
            if len(plan.plan) < 5:
                raise ValueError(f"plan too short ({len(plan.plan)} chars); need ≥ 5")

        return _check

    spec = _spec(tmp_path)
    spec = TailoringSpec(**{**spec.__dict__, "build_plan_validator": _reject_short})

    client = MockLLMClient(
        responses=[
            '{"plan":"ok"}',  # too short — should retry
            '{"plan":"ok-longer"}',  # passes
        ]
    )

    await engine.run(state, llm_client=client, spec=spec)

    assert "a" in state.outputs
    assert state.applied == [("a", "ok-longer")]
    assert not any("plan too short" in err for err in state.errors)
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_plan_validator_exhausts_retries_and_logs(tmp_path: Path) -> None:
    """When the validator never accepts a response, the item fails with the real error."""
    engine: TailoringEngine[TailorState, str, str, list[str], DummyPlan, str, Runtime] = (
        TailoringEngine()
    )
    state = TailorState(items=["a"], contexts={"a": "ctx-a"})

    def _always_reject(_s, _i, _c, _inv, _runtime):
        def _check(_plan: DummyPlan) -> None:
            raise ValueError("never good enough")

        return _check

    spec = _spec(tmp_path)
    spec = TailoringSpec(**{**spec.__dict__, "build_plan_validator": _always_reject})

    client = MockLLMClient(responses=['{"plan":"x"}'] * 3)

    await engine.run(state, llm_client=client, spec=spec)

    assert "a" not in state.outputs
    assert any("never good enough" in err for err in state.errors)
