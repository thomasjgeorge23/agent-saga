"""Testing & Chaos Engineering Framework (ChaosRunner & ReplayVerifier Fixture).

Enables chaos failure injection to verify compensation correctness and automated
replay determinism verification during unit tests.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

from .context import SagaAborted, SagaContext
from .determinism import ReplayVerifier, DeterminismResult
from .schemas import SchemaContractError

logger = logging.getLogger("agent_saga.testing")


class ChaosInjected(RuntimeError):
    """The synthetic failure ChaosRunner raises at a chosen step boundary."""


@dataclass
class MutationOutcome:
    handler: str
    mutation: str        # what was corrupted
    robust: bool         # raised (safe) vs silently accepted the bad kwargs
    schema_validated: bool  # raised SchemaContractError specifically (the ideal)
    detail: str


@dataclass
class MutationResult:
    """Outcome of mutation-testing a saga's compensation handlers."""
    outcomes: list[MutationOutcome] = field(default_factory=list)

    @property
    def tested(self) -> int:
        return len(self.outcomes)

    @property
    def fragile(self) -> list[MutationOutcome]:
        """Compensations that silently accepted corrupted kwargs -- the dangerous
        ones, which would act on a wrong charge_id / instance_id without a peep."""
        return [o for o in self.outcomes if not o.robust]

    @property
    def all_robust(self) -> bool:
        return not self.fragile

    def summary(self) -> str:
        val = sum(1 for o in self.outcomes if o.schema_validated)
        return (f"mutation test: {self.tested} mutation(s), {len(self.fragile)} fragile, "
                f"{val} schema-validated")


def default_mutations(kwargs: dict) -> Iterator[tuple[str, dict]]:
    """Corrupt each compensation kwarg the way a real API response might vary:
    a wrong-but-plausible value, a null, and a dropped field."""
    for key, value in list(kwargs.items()):
        wrong = "__mutated__" if isinstance(value, str) else (
            -987654321 if isinstance(value, (int, float)) else {"__mutated__": True})
        m = dict(kwargs); m[key] = wrong
        yield (f"wrong {key}", m)
        m = dict(kwargs); m[key] = None
        yield (f"null {key}", m)
        yield (f"drop {key}", {k: v for k, v in kwargs.items() if k != key})


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

    def __init__(self, fail_after: Optional[int] = None, *, fail_at: Optional[list[int]] = None,
                 mutation_mode: bool = False,
                 mutators: Optional[Callable[[dict], Iterator[tuple[str, dict]]]] = None):
        if fail_at is not None:
            points = sorted({int(i) for i in fail_at if int(i) >= 1})
            if not points:
                raise ValueError("fail_at must contain at least one step index >= 1")
        else:
            points = [int(fail_after) if fail_after is not None else 1]
        self.fail_points = points
        self.mutation_mode = mutation_mode
        self.mutators = mutators or default_mutations

    async def run(self, saga_fn: Callable[[SagaContext], Any], *args,
                  fail_point: Optional[int] = None, **kwargs) -> ChaosResult:
        """Run the saga once, injecting at ``fail_point`` (default: the first
        configured point). In ``mutation_mode`` this delegates to
        :meth:`mutation_run` and returns a :class:`MutationResult` instead."""
        if self.mutation_mode:
            return await self.mutation_run(saga_fn, *args, **kwargs)  # type: ignore[return-value]
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

    async def mutation_run(self, saga_fn: Callable[[SagaContext], Any], *args,
                           **kwargs) -> MutationResult:
        """Mutation-test a saga's compensations. Runs the saga forward once to
        collect each step's real compensation descriptor, then invokes every
        compensation with deliberately corrupted kwargs (wrong/null/dropped
        fields -- how a real API response varies from the happy path).

        A compensation is *robust* if it refuses corrupted input by raising
        (ideally a ``SchemaContractError`` from a typed contract). It is *fragile*
        if it completes normally on corrupted kwargs -- meaning it would act on a
        wrong charge_id / instance_id silently, the exact bug this hunts for."""
        from .decorator import saga_scope

        compensations: list = []
        async with saga_scope() as ctx:
            real_execute = ctx.execute

            async def capture_execute(*a, **kw):
                result = await real_execute(*a, **kw)
                if ctx.stack and ctx.stack[-1].compensation is not None:
                    compensations.append(ctx.stack[-1].compensation)
                return result

            ctx.execute = capture_execute  # type: ignore[method-assign]
            if asyncio.iscoroutinefunction(saga_fn):
                await saga_fn(ctx, *args, **kwargs)
            else:
                saga_fn(ctx, *args, **kwargs)

        result = MutationResult()
        for comp in compensations:
            base_kwargs = dict(getattr(comp, "kwargs", {}) or {})
            if not base_kwargs:
                continue
            for desc, mutated in self.mutators(base_kwargs):
                result.outcomes.append(await self._probe_compensation(comp, desc, mutated))
        return result

    async def _probe_compensation(self, comp: Any, desc: str, mutated_kwargs: dict) -> MutationOutcome:
        handler = getattr(comp, "handler", None) or "?"
        fn = getattr(comp, "fn", None)
        if fn is None:
            return MutationOutcome(handler, desc, robust=True, schema_validated=False,
                                   detail="no compensation fn to probe")
        try:
            outcome = fn(**mutated_kwargs)
            if inspect.isawaitable(outcome):
                await outcome
        except SchemaContractError as exc:
            return MutationOutcome(handler, desc, robust=True, schema_validated=True,
                                   detail=f"raised SchemaContractError: {exc}")
        except Exception as exc:
            return MutationOutcome(handler, desc, robust=True, schema_validated=False,
                                   detail=f"failed gracefully: {type(exc).__name__}")
        # Completed normally on corrupted kwargs -> silently accepted bad data.
        return MutationOutcome(handler, desc, robust=False, schema_validated=False,
                               detail="completed normally on corrupted kwargs (accepted bad data)")


def verify_saga_replay(records: list[dict[str, Any]]) -> DeterminismResult:
    return ReplayVerifier.verify(records)


__all__ = ["ChaosRunner", "ChaosResult", "ChaosInjected", "verify_saga_replay",
           "MutationResult", "MutationOutcome", "default_mutations"]
