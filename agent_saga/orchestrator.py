"""Parallel Fan-Out/Fan-In and Child Saga Orchestration (Temporal & Camunda Parity).

Provides ParallelSagaGroup for fan-out tool execution and fan-in consensus, and
ChildSaga for nested execution graphs with cascading compensation propagation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from .context import RollbackReport, SagaContext

logger = logging.getLogger("agent_saga.orchestrator")


class ChildSaga:
    """Represents a nested child saga linked to a parent saga execution graph."""

    def __init__(self, child_id: str, parent_ctx: SagaContext):
        self.child_id = child_id
        self.parent_ctx = parent_ctx
        self.child_ctx = SagaContext(saga_id=child_id, wal=parent_ctx.wal)

    async def execute(self, coroutine_fn: Callable[[SagaContext], Any]) -> Any:
        try:
            if asyncio.iscoroutinefunction(coroutine_fn):
                return await coroutine_fn(self.child_ctx)
            return coroutine_fn(self.child_ctx)
        except Exception as exc:
            logger.warning("Child saga %s failed, cascading rollback to child context: %r", self.child_id, exc)
            await self.child_ctx.rollback()
            raise exc


class ParallelSagaGroup:
    """Executes parallel tool tasks (fan-out) and waits for all to join (fan-in)."""

    def __init__(self, group_name: str, parent_ctx: SagaContext):
        self.group_name = group_name
        self.parent_ctx = parent_ctx
        self.tasks: list[Callable[[SagaContext], Any]] = []

    def add_task(self, task_fn: Callable[[SagaContext], Any]) -> None:
        self.tasks.append(task_fn)

    async def execute_all(self) -> list[Any]:
        """Runs all tasks concurrently in parallel child saga contexts."""
        child_contexts: list[SagaContext] = []
        coroutines = []

        for idx, task_fn in enumerate(self.tasks):
            child_ctx = SagaContext(saga_id=f"{self.parent_ctx.saga_id}-parallel-{idx}", wal=self.parent_ctx.wal)
            child_contexts.append(child_ctx)
            if asyncio.iscoroutinefunction(task_fn):
                coroutines.append(task_fn(child_ctx))
            else:
                async def _wrap(fn=task_fn, ctx=child_ctx):
                    return fn(ctx)
                coroutines.append(_wrap())

        results = await asyncio.gather(*coroutines, return_exceptions=True)

        failures = [r for r in results if isinstance(r, Exception)]
        if failures:
            logger.error("Parallel group %s had %d failure(s), rolling back group...", self.group_name, len(failures))
            for c_ctx in child_contexts:
                await c_ctx.rollback()
            raise failures[0]

        return results


__all__ = ["ChildSaga", "ParallelSagaGroup"]
