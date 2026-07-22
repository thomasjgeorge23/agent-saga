"""Camunda 8 & Zeebe Task Worker Adapter (Camunda Parity & Interoperability).

Allows Camunda / Zeebe external task workers to wrap job execution with agent-saga
anti-hallucination reality checks and WAL journal logging.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from ..context import SagaContext
from ..gate import GateContext, get_gate
from ..semantics import ActionSemantics


class SagaCamundaWorker:
    """Wraps Camunda 8 / Zeebe task worker jobs with agent-saga safety controls."""

    def __init__(self, saga_id: Optional[str] = None):
        self.saga_id = saga_id or "camunda-zeebe-saga"
        self.ctx = SagaContext(saga_id=self.saga_id)

    async def execute_job(self, task_type: str, job_handler: Callable, variables: dict[str, Any]) -> Any:
        gate_ctx = GateContext(tool=task_type, semantics=ActionSemantics.COMPENSABLE, kwargs=variables)
        decision = await get_gate().evaluate(gate_ctx)
        if decision.verdict.name == "BLOCK":
            raise RuntimeError(f"agent-saga gate BLOCKED Camunda Zeebe job {task_type!r}: {decision.reason}")

        try:
            if asyncio.iscoroutinefunction(job_handler):
                res = await job_handler(variables)
            else:
                res = job_handler(variables)
            return res
        except Exception as exc:
            await self.ctx.rollback()
            raise exc


def camunda_job_handler(task_type: str):
    """Decorator wrapping Camunda Zeebe task handlers."""
    def decorator(fn):
        async def wrapper(variables: dict[str, Any], *args, **kwargs):
            worker = SagaCamundaWorker()
            return await worker.execute_job(task_type, fn, variables)
        return wrapper
    return decorator


__all__ = ["SagaCamundaWorker", "camunda_job_handler"]
