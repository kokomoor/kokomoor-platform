"""Reusable tailoring workflow engine for multi-phase deterministic pipelines."""

from __future__ import annotations

import asyncio
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
    # Optional cacheable system prefix. Must return the SAME bytes on
    # every call within a run so the Anthropic prefix cache hits.
    build_cached_system: Callable[[StateT, RuntimeT], str | None] | None = None
    # Optional per-item semantic validator factory. When provided, the
    # engine closes over the item context and hands the returned
    # ``(plan) -> None`` callable to ``structured_complete`` so
    # domain-rule violations (word budgets, banned phrases, cross-field
    # consistency) are fed back to the LLM and retried rather than
    # swallowed as terminal errors. Must not perform I/O — it runs
    # inside the retry loop and may be called multiple times per item.
    build_plan_validator: (
        Callable[
            [StateT, ItemT, ContextT, InventoryT, RuntimeT],
            Callable[[PlanT], None],
        ]
        | None
    ) = None
    # Upper bound on concurrent in-flight LLM requests. 1 = sequential.
    concurrency: int = 1


class TailoringEngine(Generic[StateT, ItemT, ContextT, InventoryT, PlanT, DocumentT, RuntimeT]):
    """Generalized plan -> validate -> apply -> render orchestration engine."""

    async def run(
        self,
        state: StateT,
        *,
        llm_client: LLMClient,
        spec: TailoringSpec[StateT, ItemT, ContextT, InventoryT, PlanT, DocumentT, RuntimeT],
    ) -> StateT:
        if spec.should_skip(state):
            logger.info("tailoring.skip", workflow=spec.name)
            spec.on_skip(state)
            return state

        runtime = spec.prepare(state)
        inventory = spec.load_inventory(state, runtime)
        cached_system = (
            spec.build_cached_system(state, runtime)
            if spec.build_cached_system is not None
            else None
        )

        concurrency = max(1, spec.concurrency)
        semaphore = asyncio.Semaphore(concurrency)

        async def _process(item: ItemT) -> None:
            context = spec.get_context(state, item, runtime)
            if context is None:
                spec.on_missing_context(state, item, runtime)
                return

            async with semaphore:
                try:
                    spec.on_item_start(state, item, runtime)
                    inventory_view = spec.build_inventory_view(
                        state, item, context, inventory, runtime
                    )
                    prompt = spec.build_prompt(
                        state, item, context, inventory, inventory_view, runtime
                    )
                    plan_validator = None
                    if spec.build_plan_validator is not None:
                        plan_validator = spec.build_plan_validator(
                            state, item, context, inventory, runtime
                        )
                    plan = await structured_complete(
                        llm_client,
                        prompt,
                        response_model=spec.plan_model_type,
                        model=spec.get_model(state, runtime),
                        max_tokens=spec.get_max_tokens(state, runtime),
                        run_id=spec.get_run_id(state),
                        system_prefix=cached_system,
                        cache_system=cached_system is not None,
                        validator=plan_validator,
                    )
                    spec.validate_plan(state, item, context, inventory, plan, runtime)
                    document = spec.apply_plan(state, item, context, inventory, plan, runtime)
                    output_path = spec.get_output_path(state, item, runtime)
                    spec.render_document(state, item, document, output_path, runtime)
                    spec.on_item_success(state, item, plan, document, output_path, runtime)
                except Exception as exc:
                    logger.warning(
                        "tailoring.item_failed",
                        workflow=spec.name,
                        error_type=type(exc).__name__,
                        error=str(exc)[:500],
                        exc_info=True,
                    )
                    spec.on_item_error(state, item, exc, runtime)

        items = spec.get_items(state)
        if items:
            await asyncio.gather(*(_process(item) for item in items))

        spec.on_complete(state, runtime)
        return state
