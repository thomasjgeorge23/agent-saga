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

    async def _run_task(self, task_fn: Callable[[SagaContext], Any], ctx: SagaContext) -> Any:
        if not ctx.wal._task:
            await ctx.wal.start()
        if asyncio.iscoroutinefunction(task_fn):
            return await task_fn(ctx)
        return task_fn(ctx)

    async def execute_all(self) -> list[Any]:
        """Run all tasks concurrently in child saga contexts, joining per mode:

        * ``fail_fast``   -- the instant one task fails, cancel the still-running
          siblings, roll everything back, and raise. Nothing keeps running once
          the group is doomed.
        * ``fail_all``    -- let every task finish, then if any failed roll all
          back and raise. Use when siblings must not be interrupted mid-flight.
        * ``best_effort`` -- compensate only the tasks that failed and return the
          full results list (successes and exceptions in place); the group does
          not raise.
        """
        child_contexts: list[SagaContext] = [
            SagaContext(saga_id=f"{self.parent_ctx.saga_id}-parallel-{idx}",
                        wal=self.parent_ctx.wal)
            for idx in range(len(self.tasks))
        ]

        if self.mode == "fail_fast":
            return await self._execute_fail_fast(child_contexts)

        # fail_all and best_effort both wait for every task to settle.
        coroutines = [self._run_task(fn, ctx)
                      for fn, ctx in zip(self.tasks, child_contexts)]
        results = await asyncio.gather(*coroutines, return_exceptions=True)
        failures = [(idx, r) for idx, r in enumerate(results) if isinstance(r, Exception)]

        if failures:
            logger.error("Parallel group %s (mode=%s) had %d failure(s)",
                         self.group_name, self.mode, len(failures))
            if self.mode == "best_effort":
                for idx, _ in failures:
                    await child_contexts[idx].rollback()
            else:  # fail_all
                for c_ctx in child_contexts:
                    await c_ctx.rollback()
                raise failures[0][1]

        return results

    async def _execute_fail_fast(self, child_contexts: list[SagaContext]) -> list[Any]:
        tasks = [asyncio.ensure_future(self._run_task(fn, ctx))
                 for fn, ctx in zip(self.tasks, child_contexts)]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

        first_exc = next((t.exception() for t in tasks
                          if t.done() and not t.cancelled() and t.exception() is not None), None)
        if first_exc is not None:
            logger.error("Parallel group %s (mode=fail_fast): failure, cancelling siblings",
                         self.group_name)
            # Cancel siblings still running, then let their cancellations settle.
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for c_ctx in child_contexts:
                await c_ctx.rollback()
            raise first_exc

        return [t.result() for t in tasks]


__all__ = ["ChildSaga", "ParallelSagaGroup"]
