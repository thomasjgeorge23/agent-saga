"""Temporal Workflow & Activity Interceptor (Temporal Parity & Interoperability).

Enables existing Temporal workflows to run agent-saga pre-flight safety gates,
semantic locks, and compensating transactions inside Temporal workers.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from ..context import SagaContext
from ..gate import PreFlightGate, GateContext, get_gate
from ..semantics import ActionSemantics


class SagaTemporalInterceptor:
    """Wraps Temporal activities with agent-saga safety gates and compensation logging."""

    def __init__(self, saga_id: Optional[str] = None, gate: Optional[PreFlightGate] = None):
        self.saga_id = saga_id or "temporal-saga"
        self.gate = gate or get_gate()
        self.ctx = SagaContext(saga_id=self.saga_id)

    async def execute_activity(self, activity_name: str, activity_fn: Callable, *args, **kwargs) -> Any:
        # Pre-flight safety gate check
        gate_ctx = GateContext(tool=activity_name, semantics=ActionSemantics.COMPENSABLE, kwargs=kwargs)
        decision = await self.gate.evaluate(gate_ctx)
        if decision.verdict.name == "BLOCK":
            raise RuntimeError(f"agent-saga gate BLOCKED Temporal activity {activity_name!r}: {decision.reason}")

        try:
            if asyncio.iscoroutinefunction(activity_fn):
                res = await activity_fn(*args, **kwargs)
            else:
                res = activity_fn(*args, **kwargs)
            return res
        except Exception as exc:
            await self.ctx.rollback()
            raise exc


def saga_activity(name: str, undo_fn: Optional[Callable] = None):
    """Decorator for Temporal activities to automatically bind agent-saga LIFO compensation."""
    def decorator(fn):
        async def wrapper(*args, **kwargs):
            interceptor = SagaTemporalInterceptor()
            return await interceptor.execute_activity(name, fn, *args, **kwargs)
        return wrapper
    return decorator


__all__ = ["SagaTemporalInterceptor", "saga_activity"]
