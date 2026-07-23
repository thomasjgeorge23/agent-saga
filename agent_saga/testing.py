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


class ChaosInjected(RuntimeError):
    """The synthetic failure ChaosRunner raises at a chosen step boundary."""


@dataclass
class ChaosResult:
    rolled_back: bool
    compensations_executed: list[str]
    exception: Optional[BaseException]
    injected_at: Optional[int] = None
    """The 1-based step index at which the failure fired, or None if the saga
    completed before reaching a fail point (fewer steps than configured)."""


class ChaosRunner:
    """Injects a synthetic failure at a chosen saga step and reports the rollback.

    A "step" is a real ``ctx.execute(...)`` call. The failure fires immediately
    after the target step's forward action commits, so every step up to and
    including it is on the stack and must compensate -- exactly the partial
    rollback you want to assert on.

        # single point (back-compatible)
        ChaosRunner(fail_after=2)          # fail right after the 2nd step

        # a matrix of points, tested independently in one call
        runner = ChaosRunner(fail_at=[2, 5])
        results = await runner.run_all(my_saga)   # {2: ChaosResult, 5: ChaosResult}

    ``run_all`` runs the saga once per configured point (each a fresh saga), so a
    complex flow can be probed at several rollback boundaries without hand-writing
    a run per point.
    """

    def __init__(self, fail_after: Optional[int] = None, *, fail_at: Optional[list[int]] = None):
        if fail_at is not None:
            points = sorted({int(i) for i in fail_at if int(i) >= 1})
            if not points:
                raise ValueError("fail_at must contain at least one step index >= 1")
        else:
            points = [int(fail_after) if fail_after is not None else 1]
        self.fail_points = points

    async def run(self, saga_fn: Callable[[SagaContext], Any], *args,
                  fail_point: Optional[int] = None, **kwargs) -> ChaosResult:
        """Run the saga once, injecting at ``fail_point`` (default: the first
        configured point)."""
        target = fail_point if fail_point is not None else self.fail_points[0]
        from .decorator import saga_scope

        counter = {"n": 0}

        try:
            async with saga_scope() as ctx:
                real_execute = ctx.execute

                async def chaos_execute(*a, **kw):
                    counter["n"] += 1
                    idx = counter["n"]
                    result = await real_execute(*a, **kw)
                    if idx == target:
                        raise ChaosInjected(
                            f"ChaosRunner injected failure after step {idx}")
                    return result

                ctx.execute = chaos_execute  # type: ignore[method-assign]

                if asyncio.iscoroutinefunction(saga_fn):
                    await saga_fn(ctx, *args, **kwargs)
                else:
                    saga_fn(ctx, *args, **kwargs)
            # Completed without reaching the fail point.
            return ChaosResult(rolled_back=False, compensations_executed=[],
                               exception=None, injected_at=None)
        except SagaAborted as aborted:
            comps = [getattr(s, "tool", str(s))
                     for s in (getattr(aborted.report, "compensated", []) or [])]
            injected = target if isinstance(aborted.cause, ChaosInjected) else None
            return ChaosResult(rolled_back=True, compensations_executed=comps,
                               exception=aborted.cause, injected_at=injected)
        except Exception as exc:
            injected = target if isinstance(exc, ChaosInjected) else None
            return ChaosResult(rolled_back=True, compensations_executed=[],
                               exception=exc, injected_at=injected)

    async def run_all(self, saga_fn: Callable[[SagaContext], Any], *args,
                      **kwargs) -> dict[int, ChaosResult]:
        """Run the saga once per configured fail point; return {point: result}."""
        results: dict[int, ChaosResult] = {}
        for point in self.fail_points:
            results[point] = await self.run(saga_fn, *args, fail_point=point, **kwargs)
        return results


def verify_saga_replay(records: list[dict[str, Any]]) -> DeterminismResult:
    return ReplayVerifier.verify(records)


__all__ = ["ChaosRunner", "ChaosResult", "ChaosInjected", "verify_saga_replay"]
