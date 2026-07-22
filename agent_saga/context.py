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
from .observability import reset_saga_id, reset_step_id, set_saga_id, set_step_id
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
        self._obs_token = None
        self._abort_cause: Optional[tuple[str, str]] = None
        self._span_cm = None
        self._span = None
        self._tentatives: list = []
        """Resources held in PENDING for this saga's lifetime. Resolved exactly
        once at the boundary, so the failure path -- the one callers forget --
        is handled for them."""
        self._semantic_locks: list[str] = []

    # -- isolation countermeasures -----------------------------------------
    # A saga has no ACID isolation: every step commits as it runs. These are the
    # structural answers, wired into the lifecycle so they cannot be forgotten.

    def register_tentative(self, resource, *, lock: bool = False):
        """Track a TentativeResource for automatic resolution at the boundary.

        Registration is recorded in the WAL, not just in memory. A tentative
        resource that lived only in this process would be stranded PENDING
        forever by a SIGKILL, with no daemon able to see it -- exactly the
        orphan this engine exists to prevent.
        """
        from .locks import SemanticLockConflictError, get_semantic_locks

        if lock:
            manager = get_semantic_locks()
            if getattr(manager, "distributed", False):
                # A distributed lock is a network round trip and cannot be taken
                # from this synchronous call. Say so precisely rather than
                # silently skipping the lock, which would hand back a claim that
                # was never made.
                raise RuntimeError(
                    f"{type(manager).__name__} is distributed and cannot be "
                    f"acquired synchronously. Do this instead:\n"
                    f"    await ctx.acquire_semantic_lock({resource.resource_id!r})\n"
                    f"    tentative(ctx, {resource.resource_id!r}, lock=False, ...)"
                )
            # Fail fast rather than block: telling an agent the account is busy
            # beats freezing it mid-run behind another saga.
            if not manager.try_acquire(resource.resource_id, self.saga_id):
                raise SemanticLockConflictError(
                    resource.resource_id,
                    manager.owner(resource.resource_id) or "?", self.saga_id)
            self._semantic_locks.append(resource.resource_id)
        self._tentatives.append(resource)

        if not resource.recoverable:
            logger.warning(
                "tentative resource %r has no registry rollback_handler; it will "
                "resolve normally in-process, but a crash leaves it PENDING with "
                "no way for the recovery daemon to roll it back.",
                resource.resource_id)
        self.wal.append("TENTATIVE_REGISTERED", {
            "saga_id": self.saga_id, **resource.describe()})
        return resource

    async def register_tentative_durable(self, resource, *, lock: bool = False):
        """register_tentative, plus a fence.

        Use this when the tentative write is the money: the registration must be
        on disk *before* the debit, or a crash in between leaves a debit no one
        knows was provisional.
        """
        self.register_tentative(resource, lock=lock)
        await self.wal.barrier()
        return resource

    async def acquire_semantic_lock(self, resource_id: str, *,
                                    timeout: float = 5.0) -> None:
        """Claim a business resource for this saga. Released automatically when
        the saga finishes, however it finishes."""
        from .locks import get_semantic_locks

        await get_semantic_locks().acquire(resource_id, self.saga_id, timeout=timeout)
        if resource_id not in self._semantic_locks:
            self._semantic_locks.append(resource_id)

    async def _release_semantic_locks(self) -> None:
        """Release every claim this saga holds, on every exit path.

        Supports both the in-process manager (sync) and a distributed one
        (async), so swapping backends does not change the lifecycle.
        """
        if not self._semantic_locks:
            return
        import inspect

        from .locks import get_semantic_locks

        released = get_semantic_locks().release_all(self.saga_id)
        if inspect.isawaitable(released):
            released = await released
        self._semantic_locks.clear()
        if released:
            logger.info("released %d semantic lock(s): %s",
                        len(released), ", ".join(sorted(released)))

    async def _resolve_tentatives(self, *, committed: bool) -> None:
        """Move every still-pending resource to its terminal status, once.

        A callback failure is reported, never swallowed: a resource silently
        stuck in PENDING is a balance nobody will reconcile.
        """
        for resource in self._tentatives:
            if resource.resolved:
                continue
            try:
                if committed:
                    await resource.commit()
                else:
                    await resource.rollback()
                # Record the terminal status so a daemon reading this WAL later
                # knows the resource is settled and must not touch it again.
                self.wal.append("TENTATIVE_RESOLVED", {
                    "saga_id": self.saga_id,
                    "resource_id": resource.resource_id,
                    "status": resource.status.value})
            except Exception as exc:
                logger.error(
                    "tentative resource %r failed to resolve to %s: %r",
                    resource.resource_id,
                    "COMMITTED" if committed else "ROLLED_BACK", exc)
                self.wal.append("TENTATIVE_UNRESOLVED", {
                    "saga_id": self.saga_id,
                    "resource_id": resource.resource_id,
                    "target": "COMMITTED" if committed else "ROLLED_BACK",
                    "error": repr(exc)})

    # -- lease -------------------------------------------------------------
    # The recovery daemon must distinguish "this saga is still running" from
    # "this saga's process is gone". A renewed lease is the only honest signal;
    # a PID is not, because PIDs are reused.

    async def begin(self) -> None:
        # A draining system lets running sagas finish but starts no new ones.
        # Checked here rather than in the gate because the gate sees steps, and
        # refusing a *step* mid-saga would strand it half-done -- the opposite
        # of draining.
        from .killswitch import get_kill_switch

        switch = get_kill_switch()
        if switch is not None:
            switch.check_start()

        # Bind the correlation id for the whole saga, so every log line emitted
        # under this context -- forward calls, the failure, all compensations --
        # carries the same saga_id an operator can grep on.
        self._obs_token = set_saga_id(self.saga_id)
        # Root span for the whole saga. Entered manually rather than with a
        # `with` block because a saga's lifetime spans begin()..finish(), which
        # are separate calls the caller drives.
        from .observability.otel import ATTR_SAGA_ID, SPAN_SAGA, get_tracer

        self._span_cm = get_tracer().span(SPAN_SAGA, {ATTR_SAGA_ID: self.saga_id})
        self._span = self._span_cm.__enter__()
        self.wal.append("SAGA_START", {"saga_id": self.saga_id, "pid": os.getpid(),
                                       "lease_ttl": self.lease_ttl})
        await self.wal.barrier()
        self._heartbeat = asyncio.create_task(self._renew_lease())
        logger.info("saga started (pid=%s, lease_ttl=%ss)", os.getpid(), self.lease_ttl)

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
        # Resolve tentative state before the terminal record, so the WAL reads
        # in causal order. On the abort path rollback() has already rolled them
        # back; this is a no-op for anything already resolved.
        await self._resolve_tentatives(committed=not aborted)
        # Locks come off last, and unconditionally: an aborted saga must not
        # strand a resource claimed forever.
        await self._release_semantic_locks()

        self.wal.append("SAGA_ABORTED" if aborted else "SAGA_COMPLETE",
                        {"saga_id": self.saga_id, "clean": clean})
        await self.wal.barrier()
        # Close the root span with the saga's real outcome. ROLLED_BACK and
        # FAILED are distinct on purpose: one means we cleaned up, the other
        # means we could not.
        from .observability.otel import (
            ATTR_SAGA_STATUS, STATUS_COMPLETED, STATUS_FAILED, STATUS_ROLLED_BACK)

        if self._span is not None:
            status = (STATUS_COMPLETED if not aborted
                      else (STATUS_ROLLED_BACK if clean else STATUS_FAILED))
            self._span.set_attribute(ATTR_SAGA_STATUS, status)
        if self._span_cm is not None:
            self._span_cm.__exit__(None, None, None)
            self._span_cm = self._span = None

        logger.info("saga %s", "aborted (rolled back)" if aborted else "completed")
        # Unbind the correlation id last, so the line above still carries it.
        if self._obs_token is not None:
            reset_saga_id(self._obs_token)
            self._obs_token = None

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
        self._abort_cause = (type(exc).__name__, message)
        self.wal.append("SAGA_ABORT_CAUSE", {
            "saga_id": self.saga_id,
            "cause_type": type(exc).__name__,
            "cause": message if len(message) <= 2000 else message[:2000] + "…",
        })
        logger.error("rollback triggered by %s: %s", type(exc).__name__, message)

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
        fallback_action: Optional[Callable] = None,
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

        # 3. Execute, inside a child span so a trace shows which step failed.
        from .observability.otel import (
            ATTR_IS_COMPENSATION, ATTR_SEMANTICS, ATTR_STEP_ID, ATTR_TOOL,
            get_tracer, step_span_name)

        tok_saga = set_saga_id(self.saga_id)
        tok_step = set_step_id(step.step_id)
        try:
            try:
                with get_tracer().span(step_span_name(tool), {
                    ATTR_STEP_ID: step.step_id,
                    ATTR_TOOL: tool,
                    ATTR_SEMANTICS: semantics.value,
                    ATTR_IS_COMPENSATION: False,
                }):
                    result = await _invoke(forward, forward_kwargs,
                                           timeout or self.default_timeout)
            except BaseException as exc:
                if isinstance(exc, (asyncio.CancelledError, SystemExit, KeyboardInterrupt)):
                    raise
                if fallback_action is not None:
                    try:
                        import inspect
                        if inspect.iscoroutinefunction(fallback_action):
                            result = await fallback_action()
                        else:
                            result = fallback_action()

                        step.state = StepState.COMPLETED_VIA_FALLBACK
                        self.wal.append(
                            "COMPLETED_VIA_FALLBACK",
                            {
                                "saga_id": self.saga_id,
                                "step_id": step.step_id,
                                "tool": tool,
                                "semantics": semantics.value,
                                "compensation": None
                            },
                        )
                        if self.durable_commit and semantics is not ActionSemantics.REVERSIBLE:
                            await self.wal.barrier()
                        return result
                    except Exception as fallback_exc:
                        logger.error("Fallback action failed with error: %r", fallback_exc)

                # We do not know whether the effect landed. A timed-out POST to
                # Stripe may well have charged the card. Treat as UNKNOWN, not as
                # "did not happen", and still attempt idempotent compensation.
                step.state = StepState.UNKNOWN
                step.error = repr(exc)
                # Tell the breaker. Reached only when the tool actually ran and
                # raised -- a PreFlightViolation is thrown by the gate above and
                # never arrives here, which is deliberate: counting refusals would
                # trip the breaker exactly when the controls were working, and it
                # would then block the calls that were still fine.
                _tell_breaker(tool, ok=False, error=repr(exc))
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
            _tell_breaker(tool, ok=True)
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
        finally:
            reset_step_id(tok_step)
            reset_saga_id(tok_saga)

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
        logger.info("rollback starting: %d step(s) on the stack (LIFO)", len(self.stack))

        for step in reversed(self.stack):
            # Bind the step id so each compensation's logs are traceable to it.
            # Overwritten each iteration; cleared after the loop.
            set_step_id(step.step_id)
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
                from .observability.otel import (
                    ATTR_IS_COMPENSATION as _IS_COMP,
                    ATTR_STEP_ID as _SID,
                    ATTR_TOOL as _TOOL,
                    get_tracer as _tracer,
                    rollback_span_name as _rb_name,
                )

                with _tracer().span(_rb_name(step.tool), {
                    _SID: step.step_id,
                    _TOOL: step.tool,
                    _IS_COMP: True,
                }):
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
                # ASCII only: these lines print to consoles whose encoding is not
                # UTF-8 (Windows cp1252), where a dash or arrow becomes mojibake.
                logger.error("compensation FAILED for %r: %r%s", step.tool, exc,
                             " - halting rollback" if self.halt_on_compensation_failure else "")
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
            logger.info("compensated %r (%s)", step.tool, step.semantics.value)

        set_step_id(None)  # clear step correlation; the saga id stays bound

        # Tentative resources roll back with the compensations they belong to,
        # so a caller driving rollback() directly (without the boundary) still
        # gets correct state.
        await self._resolve_tentatives(committed=False)

        self.wal.append(
            "ROLLBACK_END",
            {"saga_id": self.saga_id, "clean": report.clean,
             "compensated": len(report.compensated),
             "failed": len(report.failed), "orphaned": len(report.orphaned),
             "unresolved": len(report.unresolved), "halted": report.halted},
        )
        await self.wal.barrier()
        # The one-line verdict an on-call engineer needs: clean or not, and why.
        (logger.info if report.clean else logger.error)("rollback complete: %s",
                                                         report.summary())
        return report


def _tell_breaker(tool: str, *, ok: bool, error: str = "") -> None:
    """Report an outcome to the circuit breaker, if one is installed.

    Only forward steps are reported. Compensations deliberately are not: a
    breaker that learned from rollback failures would open on the connector
    whose compensations are failing, and then refuse the remaining
    compensations -- turning a dependency outage into stranded money.
    """
    from .breaker import get_breaker

    breaker = get_breaker()
    if breaker is None:
        return
    try:
        if ok:
            breaker.record_success(tool)
        else:
            breaker.record_failure(tool, error)
    except Exception as exc:
        # Observability must never break the transaction it is observing.
        logger.warning("could not update circuit breaker for %r: %r", tool, exc)


async def _invoke(fn: Callable[..., Any], kwargs: dict, timeout: Optional[float]) -> Any:
    """Sync callables go to a worker thread. Running a blocking HTTP client on
    the event loop would stall every other in-flight agent on the process."""
    if inspect.iscoroutinefunction(fn):
        coro = fn(**kwargs)
    else:
        # The bounded, instrumented tool pool -- never the default executor, which
        # the WAL flusher would otherwise be competing for. Contextvars (and so
        # the saga correlation id) are propagated into the worker.
        from .executors import get_tool_executor

        coro = get_tool_executor().run(fn, kwargs)
    if timeout is not None:
        return await asyncio.wait_for(coro, timeout)
    return await coro


def _derive(factory: CompensationFactory, result: Any, tool: str) -> Optional[Compensation]:
    """A broken compensation factory must not mask the original failure or prevent step completion;
    uncompensated steps will be correctly identified as orphaned during rollback."""
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
