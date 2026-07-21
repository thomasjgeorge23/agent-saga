"""Saga execution context: write-ahead ordering, LIFO compensation, leases,
and an auditable report of what could *not* be undone.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .gate import GateContext, PreFlightGate, PreFlightViolation
from .semantics import (
    ActionSemantics,
    Compensation,
    CompensationFactory,
    SagaStep,
    StepState,
    _safe,
)
from .wal import AsyncWAL

logger = logging.getLogger("agent_saga")

DEFAULT_LEASE_TTL = 5.0

_WARNED_UNRECOVERABLE: set[tuple[str, str]] = set()
"""Warn once per (tool, reason) per process. This is a "your wiring is wrong"
warning, not an event log -- emitting it per call would produce thousands of
identical lines, which gets the whole logger filtered and defeats the purpose."""


@dataclass
class RollbackReport:
    """The output a compliance officer reads. `clean` is the only word that
    matters, and it is false more often than a naive undo library admits."""

    compensated: list[SagaStep] = field(default_factory=list)
    failed: list[SagaStep] = field(default_factory=list)
    orphaned: list[SagaStep] = field(default_factory=list)
    unresolved: list[SagaStep] = field(default_factory=list)
    halted: bool = False

    @property
    def clean(self) -> bool:
        return not (self.failed or self.orphaned or self.unresolved)

    def summary(self) -> str:
        if self.clean:
            return f"Rollback clean: {len(self.compensated)} step(s) compensated."
        parts = [f"{len(self.compensated)} compensated"]
        for label, steps in (
            ("FAILED", self.failed),
            ("ORPHANED (irreversible)", self.orphaned),
            ("UNRESOLVED", self.unresolved),
        ):
            if steps:
                parts.append(f"{len(steps)} {label}: {', '.join(s.tool for s in steps)}")
        return "Rollback INCOMPLETE -- " + "; ".join(parts)


class SagaAborted(Exception):
    """Raised after rollback completes, carrying the original cause and the
    report. Callers must be able to distinguish 'we cleaned up' from 'we tried'."""

    def __init__(self, cause: BaseException, report: RollbackReport):
        self.cause = cause
        self.report = report
        super().__init__(f"{type(cause).__name__}: {cause} | {report.summary()}")


class SagaContext:
    def __init__(
        self,
        gate: Optional[PreFlightGate] = None,
        wal: Optional[AsyncWAL] = None,
        *,
        halt_on_compensation_failure: bool = True,
        default_timeout: Optional[float] = None,
        saga_id: Optional[str] = None,
        lease_ttl: float = DEFAULT_LEASE_TTL,
        durable_commit: bool = True,
    ):
        self.durable_commit = durable_commit
        """Fsync the compensation descriptor before returning to the agent.
        Turning this off halves durable-path latency and makes crash recovery
        best-effort. That is a legitimate choice for low-value COMPENSABLE work
        and a terrible one for payments."""
        self.gate = gate or PreFlightGate()
        self.wal = wal or AsyncWAL()
        self.stack: list[SagaStep] = []
        self.halt_on_compensation_failure = halt_on_compensation_failure
        """Default True. If compensating step N fails, step N-1's compensation may
        operate on state that is no longer what it assumed. Continuing blindly is
        how a partial rollback becomes a worse outcome than no rollback."""
        self.default_timeout = default_timeout
        self.saga_id = saga_id or uuid.uuid4().hex
        self.lease_ttl = lease_ttl
        self._rolled_back = False
        self._heartbeat: Optional[asyncio.Task] = None

    # -- lease -------------------------------------------------------------
    # The recovery daemon must distinguish "this saga is still running" from
    # "this saga's process is gone". A renewed lease is the only honest signal;
    # a PID is not, because PIDs are reused.

    async def begin(self) -> None:
        self.wal.append("SAGA_START", {"saga_id": self.saga_id, "pid": os.getpid(),
                                       "lease_ttl": self.lease_ttl})
        await self.wal.barrier()
        self._heartbeat = asyncio.create_task(self._renew_lease())

    async def _renew_lease(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.lease_ttl / 3)
                self.wal.append("SAGA_LEASE", {"saga_id": self.saga_id, "pid": os.getpid()})
        except asyncio.CancelledError:
            raise

    async def finish(self, *, aborted: bool = False, clean: bool = True) -> None:
        if self._heartbeat:
            self._heartbeat.cancel()
            try:
                await self._heartbeat
            except asyncio.CancelledError:
                pass
            self._heartbeat = None
        self.wal.append("SAGA_ABORTED" if aborted else "SAGA_COMPLETE",
                        {"saga_id": self.saga_id, "clean": clean})
        await self.wal.barrier()

    def record_abort(self, exc: BaseException) -> None:
        """Record what triggered the rollback.

        The WAL otherwise knows only that a rollback *began*, not why -- the
        triggering exception lives in the raised SagaAborted and never reaches
        disk. This is the one point where the cause is known, so a post-mortem
        (or the time-travel debugger) can name the failure without the live
        stack. Written before ROLLBACK_START so it reads in causal order; the
        rollback's own barrier makes it durable.

        Only type and message are captured -- never a traceback, which is large
        and far more likely to carry sensitive locals. The message is still
        app-controlled text; treat it as potentially sensitive downstream.
        """
        message = str(exc)
        self.wal.append("SAGA_ABORT_CAUSE", {
            "saga_id": self.saga_id,
            "cause_type": type(exc).__name__,
            "cause": message if len(message) <= 2000 else message[:2000] + "…",
        })

    # -- execution ---------------------------------------------------------

    async def execute(
        self,
        tool: str,
        semantics: ActionSemantics,
        forward: Callable[..., Any],
        forward_kwargs: Optional[dict] = None,
        compensate: Optional[CompensationFactory] = None,
        *,
        timeout: Optional[float] = None,
        policy_args: Optional[dict] = None,
    ) -> Any:
        """`policy_args` is what the gate evaluates, and it exists because
        `forward_kwargs` is not trustworthy for policy.

        A connector that wraps its call in a closure -- which is the natural way
        to write one -- passes `forward_kwargs={}`. The amount, the table, the
        recipient are all captured in the closure and completely invisible to
        `arg_exceeds` and friends. The gate would silently pass everything.

        So policy-relevant arguments are declared explicitly. Every connector in
        this package does so, and any connector that does not is opting out of
        threshold policy for its tool.
        """
        forward_kwargs = forward_kwargs or {}
        ctx = GateContext(tool=tool, semantics=semantics,
                          kwargs={**forward_kwargs, **(policy_args or {})})

        # 1. Gate first. Nothing has happened yet; this is the only point at
        #    which refusal is free.
        await self.gate.evaluate(ctx)

        if semantics is not ActionSemantics.IRREVERSIBLE and compensate is None:
            logger.warning(
                "Tool %r declared %s but supplied no compensation factory; "
                "it will be reported as ORPHANED on rollback.",
                tool,
                semantics.value,
            )

        step = SagaStep(tool=tool, semantics=semantics)
        self.stack.append(step)

        # Under a BLOCK backpressure policy, yield until the WAL buffer has room
        # so the intent append below cannot be dropped. No-op otherwise.
        await self.wal.ensure_capacity()

        # 2. Write intent BEFORE the effect. On COMPENSABLE/IRREVERSIBLE we pay
        #    for durability -- losing the record of a charge is unacceptable;
        #    losing the record of a cache write is not.
        seq = self.wal.append(
            "STEP_INTENT",
            {
                "saga_id": self.saga_id,
                "step_id": step.step_id,
                "tool": tool,
                "semantics": semantics.value,
                "kwargs": {k: _safe(v) for k, v in forward_kwargs.items()},
            },
        )
        if semantics is not ActionSemantics.REVERSIBLE:
            await self.wal.barrier(seq)

        # 3. Execute.
        try:
            result = await _invoke(forward, forward_kwargs, timeout or self.default_timeout)
        except BaseException as exc:
            # We do not know whether the effect landed. A timed-out POST to
            # Stripe may well have charged the card. Treat as UNKNOWN, not as
            # "did not happen", and still attempt idempotent compensation.
            step.state = StepState.UNKNOWN
            step.error = repr(exc)
            if compensate is not None:
                step.compensation = _derive(compensate, None, tool)
            self.wal.append(
                "STEP_UNKNOWN",
                {"saga_id": self.saga_id, "step_id": step.step_id, "tool": tool,
                 "semantics": semantics.value, "error": repr(exc),
                 "compensation": step.compensation.describe() if step.compensation else None},
            )
            if semantics is not ActionSemantics.REVERSIBLE:
                await self.wal.barrier()
            raise

        # 4. Derive the inverse from the actual result -- the charge id, the row
        #    ids, the message ts. This is what a statically declared workflow
        #    cannot do when the agent picked the action at runtime.
        step.state = StepState.COMMITTED
        if compensate is not None:
            step.compensation = _derive(compensate, result, tool)
            self._warn_if_unrecoverable(step)

        self.wal.append(
            "STEP_COMMITTED",
            {"saga_id": self.saga_id, "step_id": step.step_id, "tool": tool,
             "semantics": semantics.value,
             "compensation": step.compensation.describe() if step.compensation else None},
        )
        # The compensation descriptor is only born here -- it needed the result.
        # If we crash before it is durable, the daemon inherits a STEP_INTENT
        # with no way to undo it, which is exactly the orphan we exist to
        # prevent. So the money path pays for a second fsync. Group commit
        # amortizes it under concurrency; at idle it roughly doubles p50.
        if self.durable_commit and semantics is not ActionSemantics.REVERSIBLE:
            await self.wal.barrier()
        return result

    def _warn_if_unrecoverable(self, step: SagaStep) -> None:
        """Say it now, while everything is still fine -- not at 3am from a
        daemon that has no way to fix it."""
        comp = step.compensation
        if comp is None or comp.recoverable:
            return
        if step.semantics is ActionSemantics.REVERSIBLE:
            # REVERSIBLE steps are already best-effort by design -- they never
            # fsync. Warning on every one of them would be pure noise.
            return
        reason = ("no registry handler name" if not comp.handler
                  else "kwargs do not survive a JSON round trip")
        if (step.tool, reason) in _WARNED_UNRECOVERABLE:
            return
        _WARNED_UNRECOVERABLE.add((step.tool, reason))
        logger.warning(
            "Step %r (%s) has an in-process-only compensation (%s). It will roll "
            "back normally, but if this process dies the effect is unrecoverable.",
            step.tool, step.semantics.value, reason,
        )

    async def rollback(self) -> RollbackReport:
        """LIFO compensation. Steps are never silently discarded -- a step that
        fails to compensate stays on the stack so a human or a retry can find it."""
        if self._rolled_back:
            raise RuntimeError("rollback() already ran on this context")
        self._rolled_back = True

        report = RollbackReport()
        self.wal.append("ROLLBACK_START", {"saga_id": self.saga_id, "steps": len(self.stack)})

        for step in reversed(self.stack):
            if report.halted:
                step.state = StepState.UNRESOLVED
                report.unresolved.append(step)
                continue

            if step.state is StepState.INTENT_LOGGED:
                continue  # gated or never executed; nothing to undo

            if not step.needs_compensation:
                continue

            if step.compensation is None:
                step.state = StepState.ORPHANED
                report.orphaned.append(step)
                self.wal.append(
                    "STEP_ORPHANED",
                    {"saga_id": self.saga_id, "step_id": step.step_id, "tool": step.tool,
                     "semantics": step.semantics.value},
                )
                logger.error(
                    "ORPHANED EFFECT: %r (%s) executed and cannot be undone.",
                    step.tool,
                    step.semantics.value,
                )
                continue

            try:
                await _invoke(step.compensation.fn, step.compensation.kwargs, None)
            except BaseException as exc:
                step.state = StepState.COMPENSATION_FAILED
                step.error = repr(exc)
                report.failed.append(step)
                self.wal.append(
                    "COMPENSATION_FAILED",
                    {"saga_id": self.saga_id, "step_id": step.step_id, "tool": step.tool,
                     "error": repr(exc),
                     "idempotency_key": step.compensation.idempotency_key},
                )
                logger.error("Compensation failed for %r: %r", step.tool, exc)
                if self.halt_on_compensation_failure:
                    report.halted = True
                continue

            step.state = StepState.COMPENSATED
            report.compensated.append(step)
            self.wal.append(
                "COMPENSATED",
                {"saga_id": self.saga_id, "step_id": step.step_id, "tool": step.tool,
                 "idempotency_key": step.compensation.idempotency_key},
            )

        self.wal.append(
            "ROLLBACK_END",
            {"saga_id": self.saga_id, "clean": report.clean,
             "compensated": len(report.compensated),
             "failed": len(report.failed), "orphaned": len(report.orphaned),
             "unresolved": len(report.unresolved), "halted": report.halted},
        )
        await self.wal.barrier()
        return report


async def _invoke(fn: Callable[..., Any], kwargs: dict, timeout: Optional[float]) -> Any:
    """Sync callables go to a worker thread. Running a blocking HTTP client on
    the event loop would stall every other in-flight agent on the process."""
    if inspect.iscoroutinefunction(fn):
        coro = fn(**kwargs)
    else:
        coro = asyncio.to_thread(lambda: fn(**kwargs))
    if timeout is not None:
        return await asyncio.wait_for(coro, timeout)
    return await coro


def _derive(factory: CompensationFactory, result: Any, tool: str) -> Optional[Compensation]:
    """A broken compensation factory must not mask the original failure."""
    try:
        comp = factory(result)
    except Exception as exc:
        logger.error("Compensation factory for %r raised: %r", tool, exc)
        return None
    if comp is not None and not isinstance(comp, Compensation):
        logger.error("Compensation factory for %r returned %r, expected Compensation", tool, type(comp))
        return None
    return comp


__all__ = ["SagaContext", "RollbackReport", "SagaAborted", "DEFAULT_LEASE_TTL"]
