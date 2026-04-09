"""Reusable tailoring workflow engine for multi-phase deterministic pipelines."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

import structlog
from pydantic import BaseModel

from core.llm.structured import structured_complete

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from core.llm.protocol import LLMClient

logger = structlog.get_logger(__name__)

StateT = TypeVar("StateT")
ItemT = TypeVar("ItemT")
ContextT = TypeVar("ContextT")
InventoryT = TypeVar("InventoryT")
PlanT = TypeVar("PlanT", bound=BaseModel)
DocumentT = TypeVar("DocumentT")
RuntimeT = TypeVar("RuntimeT")


@dataclass(frozen=True)
class TailoringSpec(Generic[StateT, ItemT, ContextT, InventoryT, PlanT, DocumentT, RuntimeT]):
    """Spec hooks for artifact-specific tailoring specialization."""

    name: str
    plan_model_type: type[PlanT]
    prepare: Callable[[StateT], RuntimeT]
    should_skip: Callable[[StateT], bool]
    on_skip: Callable[[StateT], None]
    get_items: Callable[[StateT], list[ItemT]]
    load_inventory: Callable[[StateT, RuntimeT], InventoryT]
    get_context: Callable[[StateT, ItemT, RuntimeT], ContextT | None]
    on_missing_context: Callable[[StateT, ItemT, RuntimeT], None]
    on_item_start: Callable[[StateT, ItemT, RuntimeT], None]
    build_inventory_view: Callable[[StateT, ItemT, ContextT, InventoryT, RuntimeT], str]
    build_prompt: Callable[[StateT, ItemT, ContextT, InventoryT, str, RuntimeT], str]
    get_run_id: Callable[[StateT], str]
    get_model: Callable[[StateT, RuntimeT], str | None]
    get_max_tokens: Callable[[StateT, RuntimeT], int]
    validate_plan: Callable[[StateT, ItemT, ContextT, InventoryT, PlanT, RuntimeT], None]
    apply_plan: Callable[[StateT, ItemT, ContextT, InventoryT, PlanT, RuntimeT], DocumentT]
    get_output_path: Callable[[StateT, ItemT, RuntimeT], Path]
    render_document: Callable[[StateT, ItemT, DocumentT, Path, RuntimeT], None]
    on_item_success: Callable[[StateT, ItemT, PlanT, DocumentT, Path, RuntimeT], None]
    on_item_error: Callable[[StateT, ItemT, Exception, RuntimeT], None]
    on_complete: Callable[[StateT, RuntimeT], None]


class TailoringEngine(Generic[StateT, ItemT, ContextT, InventoryT, PlanT, DocumentT, RuntimeT]):
    """Generalized plan -> validate -> apply -> render orchestration engine."""

    async def run(
        self,
        state: StateT,
        *,
        llm_client: LLMClient,
        spec: TailoringSpec[StateT, ItemT, ContextT, InventoryT, PlanT, DocumentT, RuntimeT],
    ) -> StateT:
        runtime = spec.prepare(state)
        if spec.should_skip(state):
            logger.info("tailoring.skip", workflow=spec.name)
            spec.on_skip(state)
            return state

        inventory = spec.load_inventory(state, runtime)
        for item in spec.get_items(state):
            context = spec.get_context(state, item, runtime)
            if context is None:
                spec.on_missing_context(state, item, runtime)
                continue

            try:
                spec.on_item_start(state, item, runtime)
                inventory_view = spec.build_inventory_view(state, item, context, inventory, runtime)
                prompt = spec.build_prompt(state, item, context, inventory, inventory_view, runtime)
                plan = await structured_complete(
                    llm_client,
                    prompt,
                    response_model=spec.plan_model_type,
                    model=spec.get_model(state, runtime),
                    max_tokens=spec.get_max_tokens(state, runtime),
                    run_id=spec.get_run_id(state),
                )
                spec.validate_plan(state, item, context, inventory, plan, runtime)
                document = spec.apply_plan(state, item, context, inventory, plan, runtime)
                output_path = spec.get_output_path(state, item, runtime)
                spec.render_document(state, item, document, output_path, runtime)
                spec.on_item_success(state, item, plan, document, output_path, runtime)
            except Exception as exc:
                spec.on_item_error(state, item, exc, runtime)

        spec.on_complete(state, runtime)
        return state
