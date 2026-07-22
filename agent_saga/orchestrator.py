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

    def __init__(self, group_name: str, parent_ctx: SagaContext, mode: str = "fail_fast"):
        self.group_name = group_name
        self.parent_ctx = parent_ctx
        self.mode = mode.lower()  # fail_fast, fail_all, best_effort
        self.tasks: list[Callable[[SagaContext], Any]] = []

    def add_task(self, task_fn: Callable[[SagaContext], Any]) -> None:
        self.tasks.append(task_fn)

    async def execute_all(self) -> list[Any]:
        """Runs all tasks concurrently in parallel child saga contexts according to mode."""
        child_contexts: list[SagaContext] = []
        coroutines = []

        for idx, task_fn in enumerate(self.tasks):
            child_ctx = SagaContext(saga_id=f"{self.parent_ctx.saga_id}-parallel-{idx}", wal=self.parent_ctx.wal)
            child_contexts.append(child_ctx)
            if asyncio.iscoroutinefunction(task_fn):
                async def _wrap_async(fn=task_fn, ctx=child_ctx):
                    if not ctx.wal._task:
                        await ctx.wal.start()
                    return await fn(ctx)
                coroutines.append(_wrap_async())
            else:
                async def _wrap_sync(fn=task_fn, ctx=child_ctx):
                    if not ctx.wal._task:
                        await ctx.wal.start()
                    return fn(ctx)
                coroutines.append(_wrap_sync())

        results = await asyncio.gather(*coroutines, return_exceptions=True)

        failures = [(idx, r) for idx, r in enumerate(results) if isinstance(r, Exception)]

        if failures:
            logger.error("Parallel group %s (mode=%s) had %d failure(s)", self.group_name, self.mode, len(failures))

            if self.mode == "best_effort":
                # Rollback only failed child contexts; return results list with exceptions
                for idx, exc in failures:
                    await child_contexts[idx].rollback()
            elif self.mode == "fail_all" or self.mode == "fail_fast":
                # Rollback all child contexts
                for c_ctx in child_contexts:
                    await c_ctx.rollback()
                raise failures[0][1]

        return results


__all__ = ["ChildSaga", "ParallelSagaGroup"]
