"""Testing & Chaos Engineering Framework (ChaosRunner & ReplayVerifier Fixture).

Enables chaos failure injection to verify compensation correctness and automated
replay determinism verification during unit tests.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .context import SagaAborted, SagaContext
from .determinism import ReplayVerifier, DeterminismResult

logger = logging.getLogger("agent_saga.testing")


@dataclass
class ChaosResult:
    rolled_back: bool
    compensations_executed: list[str]
    exception: Optional[BaseException]


class ChaosRunner:
    """Injects simulated failures into sagas at designated step thresholds."""

    def __init__(self, fail_after: int = 1):
        self.fail_after = fail_after

    async def run(self, saga_fn: Callable[[SagaContext], Any], *args, **kwargs) -> ChaosResult:
        from .decorator import saga_scope

        step_count = 0
        executed_comps = []

        try:
            async with saga_scope() as ctx:
                step_count += 1
                if step_count >= self.fail_after:
                    raise RuntimeError(f"ChaosRunner injected failure at step {step_count}")
                if asyncio.iscoroutinefunction(saga_fn):
                    await saga_fn(ctx, *args, **kwargs)
                else:
                    saga_fn(ctx, *args, **kwargs)
            return ChaosResult(rolled_back=False, compensations_executed=[], exception=None)
        except SagaAborted as aborted:
            executed_comps = [getattr(s, "tool", str(s)) for s in (getattr(aborted.report, "compensated", []) or [])]
            return ChaosResult(rolled_back=True, compensations_executed=executed_comps, exception=aborted.cause)
        except Exception as exc:
            return ChaosResult(rolled_back=True, compensations_executed=[], exception=exc)


def verify_saga_replay(records: list[dict[str, Any]]) -> DeterminismResult:
    return ReplayVerifier.verify(records)


__all__ = ["ChaosRunner", "ChaosResult", "verify_saga_replay"]
