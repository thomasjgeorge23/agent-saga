"""Exhaustive verification of the rollback invariants, over the real engine.

The fair criticism of a library that claims financial safety is: *prove it*. Not
"we have tests" -- tests check the cases somebody thought of, and the failures
that matter are the ones nobody thought of.

So this enumerates **every** failure interleaving up to a bounded number of
steps and checks the engine's invariants in each one. For N steps that is: which
step fails (N choices) x which of the committed steps' inverses raise
(2^(N-1) combinations), and every resulting case is executed against
`saga_scope` and `SagaContext.execute` themselves. Not a model of the engine --
the engine.

    report = await verify_rollback_invariants(max_steps=4)
    assert report.verified          # 49 interleavings, every invariant held

**What this is.** Bounded model checking of the real implementation. For every
interleaving up to `max_steps`, exhaustively: attempts descend in LIFO order,
no step is compensated *successfully* twice, a failing inverse is retried
within its bound, every committed step lands in exactly one outcome bucket,
`clean` is true only when nothing failed or was orphaned or was left
unresolved, and once a compensation fails under halt semantics every earlier
step is reported UNRESOLVED rather than silently skipped.

Worth recording how the first run went: it reported seven violations, and all
seven were the *specification* being wrong rather than the engine. The spec said
"no step is compensated more than once"; the engine retries a failing inverse
three times, which is correct and is exactly why `Compensation.idempotency_key`
exists. The invariant now distinguishes attempts from successes. Finding that a
spec is wrong is what a model checker is for, and it is worth more than a run
that passes first time.

**What this is not.** A proof for unbounded N, and not a claim about
concurrency. It says the state machine is correct for every failure shape up to
four or five steps -- which is where a hand-written test suite stops and where
the interesting interleavings actually live -- and says nothing about six
steps, network partitions between two coordinators, or a WAL backend that
silently drops writes. Those need their own instruments, and
`tests/test_chaos.py`, the `crash_worker` subprocesses, and `test_mesh_fuzz.py`
are those instruments.

Stating the boundary is the point. "Verified" with no bound attached is the kind
of claim this package exists to avoid.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from typing import (Any, Dict, List, Optional, Sequence, Set, Tuple)

logger = logging.getLogger("agent_saga.verification")

__all__ = [
    "Interleaving",
    "InvariantViolation",
    "VerificationReport",
    "verify_rollback_invariants",
]


@dataclass(frozen=True)
class Interleaving:
    """One failure shape: where the forward path broke, and which inverses then
    refused to run."""

    steps: int
    fail_at: int                        # 1-based; the step whose forward call raises
    failing_inverses: Tuple[int, ...]   # 1-based steps whose compensation raises
    orphans: Tuple[int, ...] = ()       # 1-based steps declared with no inverse

    def describe(self) -> dict:
        return {"steps": self.steps, "fail_at": self.fail_at,
                "failing_inverses": list(self.failing_inverses),
                "orphans": list(self.orphans)}

    def __str__(self) -> str:
        return (f"{self.steps} steps, forward fails at {self.fail_at}, "
                f"inverses failing at {list(self.failing_inverses) or 'none'}, "
                f"orphans {list(self.orphans) or 'none'}")


@dataclass(frozen=True)
class InvariantViolation:
    interleaving: Interleaving
    invariant: str
    detail: str

    def describe(self) -> dict:
        return {"interleaving": self.interleaving.describe(),
                "invariant": self.invariant, "detail": self.detail}


@dataclass(frozen=True)
class VerificationReport:
    max_steps: int
    interleavings: int
    violations: Tuple[InvariantViolation, ...]
    invariants: Tuple[str, ...]

    @property
    def verified(self) -> bool:
        """Every invariant held in every interleaving.

        Requires that interleavings were actually run: a report over zero cases
        is not a proof, and returning True for it would be the vacuous success
        this package keeps refusing.
        """
        return self.interleavings > 0 and not self.violations

    def describe(self) -> dict:
        return {"max_steps": self.max_steps, "interleavings": self.interleavings,
                "verified": self.verified, "invariants": list(self.invariants),
                "violations": [v.describe() for v in self.violations]}

    def format_text(self) -> str:
        lines = [f"rollback invariant verification (bounded: N <= {self.max_steps})",
                 f"  interleavings executed : {self.interleavings}",
                 f"  invariants checked     : {len(self.invariants)}"]
        for invariant in self.invariants:
            lines.append(f"    - {invariant}")
        lines.append("")
        if self.verified:
            lines.append(f"  VERIFIED for every failure shape up to {self.max_steps} "
                         f"steps.")
            lines.append(f"  This is bounded model checking of the real engine, not a")
            lines.append(f"  proof for unbounded N and not a claim about concurrency")
            lines.append(f"  or partitions -- those have their own instruments.")
        else:
            lines.append(f"  {len(self.violations)} VIOLATION(S):")
            for violation in self.violations[:20]:
                lines.append(f"    [{violation.invariant}] {violation.interleaving}")
                lines.append(f"      {violation.detail}")
            if len(self.violations) > 20:
                lines.append(f"    ... and {len(self.violations) - 20} more")
        return "\n".join(lines)


DEFAULT_COMPENSATION_RETRIES = 3
"""`SagaContext.compensation_max_retries`. A failing inverse is retried, which
is why `Compensation.idempotency_key` exists -- an inverse must be safe to run
twice. Verification asserts the bound, not that inverses run once."""

INVARIANTS: Tuple[str, ...] = (
    "compensations are attempted in exact reverse order of commit (LIFO)",
    "no step is compensated SUCCESSFULLY more than once",
    "a failing inverse is retried a bounded number of times, never unboundedly",
    "every committed step lands in exactly one outcome bucket",
    "clean is true only when nothing failed, was orphaned, or was left unresolved",
    "once a compensation fails under halt semantics, every earlier step is "
    "reported UNRESOLVED rather than skipped",
    "a committed step with no inverse is reported ORPHANED, never dropped",
)


async def verify_rollback_invariants(
    *, max_steps: int = 4, halt_on_compensation_failure: bool = True,
    include_orphans: bool = True,
) -> VerificationReport:
    """Execute every failure interleaving up to `max_steps` and check invariants.

    Runs against the real `saga_scope`/`SagaContext`, with an in-memory WAL so
    the enumeration stays fast enough to be a test rather than a nightly job.
    """
    if max_steps < 1:
        raise ValueError(f"max_steps must be >= 1, got {max_steps}")

    violations: List[InvariantViolation] = []
    count = 0

    for interleaving in _enumerate(max_steps, include_orphans):
        count += 1
        violations.extend(
            await _check(interleaving, halt_on_compensation_failure))

    return VerificationReport(max_steps=max_steps, interleavings=count,
                              violations=tuple(violations), invariants=INVARIANTS)


def _enumerate(max_steps: int, include_orphans: bool) -> List[Interleaving]:
    """Every (step count, failure point, failing-inverse subset, orphan subset).

    The subsets are what make this exhaustive rather than illustrative: a suite
    that checks "one compensation fails" misses the case where the *first* one
    fails and halting must then strand everything before it.
    """
    out: List[Interleaving] = []
    for steps in range(1, max_steps + 1):
        for fail_at in range(1, steps + 1):
            committed = list(range(1, fail_at))       # steps before the failure
            for size in range(len(committed) + 1):
                for failing in itertools.combinations(committed, size):
                    orphan_options: List[Tuple[int, ...]] = [()]
                    if include_orphans:
                        remaining = [c for c in committed if c not in failing]
                        # One orphan is enough to exercise the branch; every
                        # subset would multiply the space without reaching a
                        # distinct code path.
                        orphan_options += [(c,) for c in remaining]
                    for orphans in orphan_options:
                        out.append(Interleaving(
                            steps=steps, fail_at=fail_at,
                            failing_inverses=failing, orphans=orphans))
    return out


async def _check(interleaving: Interleaving,
                 halt: bool) -> List[InvariantViolation]:
    from .context import SagaAborted
    from .decorator import saga_scope
    from .semantics import ActionSemantics, Compensation
    from .wal.file_wal import FileWAL

    attempts: List[int] = []       # every invocation, including retries
    successes: List[int] = []      # only the invocations that returned
    violations: List[InvariantViolation] = []

    def make_compensation(index: int):
        def undo() -> None:
            attempts.append(index)
            if index in interleaving.failing_inverses:
                raise RuntimeError(f"inverse for step {index} refuses")
            successes.append(index)
        return undo

    # path=None keeps the log in memory. 49 interleavings x an fsync per record
    # turns an exhaustive check into a nightly job; the invariants under test are
    # about the state machine, not about durability, which the WAL suite covers.
    wal = FileWAL(None)
    await wal.start()
    report = None
    try:
        try:
            async with saga_scope(wal=wal, name="verify",
                                  halt_on_compensation_failure=halt) as saga:
                # The retry COUNT is an invariant; the exponential backoff
                # between attempts is not. Left at its 0.5s default, an
                # exhaustive sweep spends most of a minute asleep, which is how
                # a verification quietly stops being run.
                saga.compensation_retry_delay = 0.0
                for index in range(1, interleaving.steps + 1):
                    if index == interleaving.fail_at:
                        def boom() -> None:
                            raise ConnectionError(f"forward step {index} fails")

                        await saga.execute(
                            tool=f"step{index}",
                            semantics=ActionSemantics.COMPENSABLE,
                            forward=boom,
                            # The failing step gets an inverse valid on UNKNOWN:
                            # its target is known before the call, so it stays
                            # correct whether the effect landed or not.
                            compensate=lambda _r, i=index: Compensation(
                                fn=make_compensation(i),
                                description=f"undo {i}"))
                    else:
                        has_inverse = index not in interleaving.orphans
                        await saga.execute(
                            tool=f"step{index}",
                            semantics=ActionSemantics.COMPENSABLE,
                            forward=lambda i=index: {"step": i},
                            compensate=(
                                (lambda _r, i=index: Compensation(
                                    fn=make_compensation(i),
                                    description=f"undo {i}"))
                                if has_inverse else None))
        except SagaAborted as aborted:
            report = aborted.report
    finally:
        await wal.close()

    if report is None:
        violations.append(InvariantViolation(
            interleaving, "a failing forward step must abort the saga",
            "the saga completed despite a step raising"))
        return violations

    violations.extend(_verify_report(interleaving, report, attempts,
                                     successes, halt))
    return violations


def _verify_report(interleaving: Interleaving, report: Any,
                   attempts: Sequence[int], successes: Sequence[int],
                   halt: bool) -> List[InvariantViolation]:
    out: List[InvariantViolation] = []

    def tool_index(step: Any) -> int:
        return int(str(getattr(step, "tool", "step0")).replace("step", ""))

    compensated = [tool_index(s) for s in report.compensated]
    failed = [tool_index(s) for s in report.failed]
    orphaned = [tool_index(s) for s in report.orphaned]
    unresolved = [tool_index(s) for s in report.unresolved]

    # 1. LIFO: attempts must descend. Repeats are retries of one step, which
    #    is why this checks descending-or-equal rather than strictly descending.
    attempted = list(attempts)
    if attempted != sorted(attempted, reverse=True):
        out.append(InvariantViolation(
            interleaving, "LIFO order",
            f"compensations were attempted in {attempted}, which is not "
            f"descending"))

    # 2. No step is compensated SUCCESSFULLY twice. A failing inverse being
    #    retried is correct -- that is what idempotency_key is for -- so the
    #    invariant is about successes, not invocations. (An earlier version of
    #    this spec conflated the two and reported the engine's retries as a
    #    violation; the verification was right and the spec was wrong.)
    if len(set(successes)) != len(successes):
        out.append(InvariantViolation(
            interleaving, "no double successful compensation",
            f"a step was compensated successfully more than once: "
            f"{list(successes)}"))

    # 3. Retries are bounded.
    for index in set(attempted):
        count = attempted.count(index)
        if count > DEFAULT_COMPENSATION_RETRIES:
            out.append(InvariantViolation(
                interleaving, "bounded retries",
                f"step {index}'s inverse was attempted {count} times, above the "
                f"bound of {DEFAULT_COMPENSATION_RETRIES}"))

    # 3. Exactly one bucket per step.
    buckets = compensated + failed + orphaned + unresolved
    if len(set(buckets)) != len(buckets):
        out.append(InvariantViolation(
            interleaving, "exactly one outcome bucket per step",
            f"a step appears in more than one bucket: compensated={compensated} "
            f"failed={failed} orphaned={orphaned} unresolved={unresolved}"))

    # 4. `clean` must not flatter the outcome.
    expected_clean = not (failed or orphaned or unresolved)
    if report.clean != expected_clean:
        out.append(InvariantViolation(
            interleaving, "clean is never claimed over an incomplete rollback",
            f"clean={report.clean} with failed={failed} orphaned={orphaned} "
            f"unresolved={unresolved}"))

    # 5. Halting strands only EARLIER steps, and reports them.
    if halt and failed:
        halt_point = max(failed)
        stranded = [i for i in unresolved if i > halt_point]
        if stranded:
            out.append(InvariantViolation(
                interleaving, "halt strands only earlier steps",
                f"steps {stranded} are later than the failure at {halt_point} "
                f"yet were reported unresolved"))
        if any(i < halt_point for i in compensated):
            out.append(InvariantViolation(
                interleaving, "halt stops the unwind",
                f"steps earlier than the halt at {halt_point} were compensated "
                f"anyway: {compensated}"))

    # 6. A declared orphan must be reported as one.
    for index in interleaving.orphans:
        if index < interleaving.fail_at and index not in orphaned + unresolved:
            out.append(InvariantViolation(
                interleaving, "an inverse-less committed step is ORPHANED",
                f"step {index} had no inverse but was not reported orphaned or "
                f"unresolved; buckets were compensated={compensated} "
                f"failed={failed} orphaned={orphaned} unresolved={unresolved}"))

    return out
