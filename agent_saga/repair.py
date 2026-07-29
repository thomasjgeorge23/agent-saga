"""Surgical repair: fix one step and carry on, instead of undoing everything.

All-or-nothing is the right default and the wrong only option. A six-step
onboarding that fails at step four because a postcode was malformed does not
need the payment reversed and the account deleted -- it needs the postcode
fixed and steps five and six run. Rolling back is correct, expensive, and
often not what anyone wanted.

This module is the escape hatch, built so it cannot quietly become a hole in
the guarantee it sits inside.

**The precondition that makes it safe.** Resuming means keeping the effects of
steps 1..k-1. If the resumed portion then fails, those retained steps must
still be undoable -- otherwise repairing has deliberately manufactured the
orphan the engine exists to prevent. So a session refuses to resume unless
every retained step carries a **registry-backed** compensation: a handler name
and JSON kwargs the WAL can hand to any process. An in-process closure is not
good enough, and the refusal names each offending step rather than degrading
into a warning nobody reads.

**Resume inherits the past.** The continuation runs in a fresh saga whose stack
is pre-loaded with the retained steps, compensations reconstructed from the log.
A failure after the repair unwinds the *whole* transaction, not just the part
that ran after a human touched it. That is the difference between resuming a
transaction and starting a second one that happens to follow the first.

**The hatch is audited, because an unaudited hatch voids the audit.** Opening a
session, amending arguments, declaring a step manually resolved, resuming, and
abandoning are all WAL events carrying the operator, the reason, and the before
and after values. `agent-saga replay` and `agent-saga graph` show them like any
other step, so a reviewer sees the human in the loop rather than an unexplained
gap in the chain.

    session = RepairSession.open(records, saga_id,
                                 operator="ops@example.com",
                                 reason="malformed postcode from the model")
    print(session.format_text())          # what is kept, what failed, blockers
    session.amend(postcode="SW1A 1AA")    # recorded, with the old value
    await session.resume(continuation, wal=wal)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import (Any, Awaitable, Callable, Dict, List, Mapping, Optional,
                    Sequence, Tuple)

from .semantics import ActionSemantics, Compensation, SagaStep, StepState

logger = logging.getLogger("agent_saga.repair")

__all__ = [
    "RepairBlocked",
    "RepairSession",
    "RetainedStep",
]


class RepairBlocked(RuntimeError):
    """The saga cannot be surgically resumed. Carries every reason, because a
    partial answer sends an operator round the loop once per blocker."""

    def __init__(self, blockers: Sequence[str]):
        self.blockers = list(blockers)
        super().__init__("cannot resume this saga:\n  - " + "\n  - ".join(blockers))


@dataclass
class RetainedStep:
    """A committed step whose effect the repair will keep."""

    step_id: str
    tool: str
    semantics: str
    compensation: Optional[Mapping[str, Any]]      # the WAL descriptor

    @property
    def recoverable(self) -> bool:
        """Whether this step could still be undone from the log alone."""
        desc = self.compensation
        return bool(desc and desc.get("handler") and desc.get("recoverable"))

    def describe(self) -> dict:
        return {"step_id": self.step_id, "tool": self.tool,
                "semantics": self.semantics, "recoverable": self.recoverable}


class RepairSession:
    """One human-in-the-loop repair of one failed saga."""

    def __init__(self, saga_id: str, retained: List[RetainedStep],
                 failure: Optional[RetainedStep], failure_args: Dict[str, Any],
                 *, operator: str, reason: str, terminal: Optional[str]):
        self.saga_id = saga_id
        self.retained = retained
        self.failure = failure
        self.operator = operator
        self.reason = reason
        self.terminal = terminal
        self._args = dict(failure_args)
        self._original_args = dict(failure_args)
        self._amendments: List[Dict[str, Any]] = []
        self._manual_notes: List[str] = []
        self._used = False

    # -- construction ---------------------------------------------------------------

    @classmethod
    def open(cls, records: Sequence[Mapping[str, Any]], saga_id: str, *,
             operator: str, reason: str) -> "RepairSession":
        """Reconstruct a failed saga from its WAL records.

        `operator` and `reason` are required, not optional with a default. A
        repair that cannot say who authorised it and why is exactly the
        unattributable state change an audit log exists to make impossible.
        """
        if not operator or not operator.strip():
            raise ValueError(
                "operator is required: a surgical repair edits a transaction "
                "mid-flight, and an unattributable edit voids the audit trail "
                "it is being made inside")
        if not reason or not reason.strip():
            raise ValueError("reason is required, for the same audit reason as operator")

        retained: Dict[str, RetainedStep] = {}
        order: List[str] = []
        failure: Optional[RetainedStep] = None
        failure_args: Dict[str, Any] = {}
        terminal: Optional[str] = None
        seen = False

        for record in records:
            if not isinstance(record, Mapping) or record.get("saga_id") != saga_id:
                continue
            seen = True
            event = record.get("event")
            step_id = record.get("step_id")

            if event in ("SAGA_COMPLETE", "SAGA_ABORTED"):
                terminal = event
            elif event == "STEP_INTENT" and step_id:
                failure_args[str(step_id)] = dict(record.get("kwargs") or {})
            elif event == "STEP_COMMITTED" and step_id:
                if str(step_id) not in retained:
                    order.append(str(step_id))
                retained[str(step_id)] = RetainedStep(
                    step_id=str(step_id), tool=str(record.get("tool", "unknown")),
                    semantics=str(record.get("semantics", "COMPENSABLE")),
                    compensation=record.get("compensation"))
            elif event == "STEP_UNKNOWN" and step_id:
                failure = RetainedStep(
                    step_id=str(step_id), tool=str(record.get("tool", "unknown")),
                    semantics=str(record.get("semantics", "COMPENSABLE")),
                    compensation=record.get("compensation"))

        if not seen:
            raise LookupError(f"no records for saga {saga_id!r} in this log")

        kept = [retained[s] for s in order]
        args = failure_args.get(failure.step_id, {}) if failure else {}
        return cls(saga_id, kept, failure, args,
                   operator=operator, reason=reason, terminal=terminal)

    # -- inspection ------------------------------------------------------------------

    @property
    def blockers(self) -> Tuple[str, ...]:
        """Every reason this saga cannot be surgically resumed."""
        problems: List[str] = []

        if self.terminal == "SAGA_COMPLETE":
            problems.append(
                "this saga already completed successfully; there is nothing to resume")
        if self.terminal == "SAGA_ABORTED":
            problems.append(
                "this saga already rolled back. Its effects are undone, so "
                "resuming would run later steps on top of a world that no "
                "longer has the earlier ones. Start a new saga instead.")

        unrecoverable = [s for s in self.retained if not s.recoverable]
        if unrecoverable:
            problems.append(
                "these retained steps have no registry-backed compensation, so "
                "keeping them means they could never be undone if the resumed "
                "part fails: "
                + ", ".join(f"{s.tool} ({s.step_id[:8]})" for s in unrecoverable)
                + ". Give them @compensator handlers with JSON kwargs, or roll "
                  "back instead of repairing.")

        if self._used:
            problems.append("this session has already been resumed or abandoned")

        return tuple(problems)

    @property
    def can_resume(self) -> bool:
        return not self.blockers

    @property
    def args(self) -> Dict[str, Any]:
        """The failed step's arguments, including any amendments."""
        return dict(self._args)

    def describe(self) -> dict:
        return {
            "saga_id": self.saga_id, "operator": self.operator,
            "reason": self.reason, "can_resume": self.can_resume,
            "blockers": list(self.blockers),
            "retained": [s.describe() for s in self.retained],
            "failure": self.failure.describe() if self.failure else None,
            "amendments": list(self._amendments),
            "manual_notes": list(self._manual_notes),
        }

    def format_text(self) -> str:
        lines = [f"repair session for saga {self.saga_id}",
                 f"  operator : {self.operator}",
                 f"  reason   : {self.reason}", ""]
        lines.append(f"  retained ({len(self.retained)} step(s) whose effects are kept):")
        for step in self.retained:
            mark = "  ok " if step.recoverable else "  !! "
            lines.append(f"  {mark}{step.tool} [{step.semantics}]"
                         + ("" if step.recoverable
                            else "  <- no registry-backed inverse"))
        if self.failure:
            lines.append("")
            lines.append(f"  failed at: {self.failure.tool}")
            if self._amendments:
                for amendment in self._amendments:
                    lines.append(f"    amended {amendment['field']}: "
                                 f"{amendment['from']!r} -> {amendment['to']!r}")
        if self.blockers:
            lines.append("")
            lines.append("  BLOCKED:")
            for blocker in self.blockers:
                lines.append(f"    - {blocker}")
        else:
            lines.append("")
            lines.append("  ready to resume")
        return "\n".join(lines)

    # -- the repair itself ---------------------------------------------------------------

    def amend(self, **changes: Any) -> "RepairSession":
        """Change the failed step's arguments, recording the old values.

        The before value travels with the amendment because "the operator
        changed the postcode" and "the operator changed the amount from 50 to
        5000" are different events, and only one of them is routine.
        """
        for field_name, value in changes.items():
            self._amendments.append({
                "field": field_name,
                "from": self._args.get(field_name),
                "to": value,
                "at": time.time(),
            })
            self._args[field_name] = value
        return self

    def mark_resolved(self, note: str) -> "RepairSession":
        """Record that a human fixed something outside the system -- a row
        corrected by hand, a stuck queue drained. Recorded rather than
        inferred, because the log should not have to guess why the second
        attempt worked."""
        if not note.strip():
            raise ValueError("a note is required: an unexplained manual "
                             "resolution is indistinguishable from a mistake")
        self._manual_notes.append(note)
        return self

    async def resume(self, continuation: Callable[[Any], Awaitable[Any]], *,
                     wal: Any, gate: Any = None) -> Any:
        """Run `continuation` in a saga that inherits the retained steps.

        `continuation(ctx)` performs the remaining work through `ctx.execute`,
        exactly as the original saga did. If it raises, the rollback covers the
        retained steps too -- so a repair that goes wrong is still a whole
        transaction, not a half of one nobody owns.
        """
        if self.blockers:
            raise RepairBlocked(self.blockers)

        from .decorator import saga_scope
        from .registry import resolve

        self._used = True
        async with saga_scope(wal=wal, gate=gate, name=f"repair:{self.saga_id}") as ctx:
            ctx.wal.append("REPAIR_OPENED", {
                "saga_id": ctx.saga_id, "repaired_saga_id": self.saga_id,
                "operator": self.operator, "reason": self.reason,
                "retained_steps": [s.describe() for s in self.retained],
                "amendments": list(self._amendments),
                "manual_notes": list(self._manual_notes),
            })

            # Inherit the retained steps so a later failure unwinds everything.
            # Their compensations are rebuilt from the log, which is why the
            # registry-backed precondition above is not negotiable.
            for step in self.retained:
                desc = dict(step.compensation or {})
                handler_name = desc.get("handler")
                handler = resolve(handler_name) if handler_name else None
                if handler is None:
                    raise RepairBlocked([
                        f"handler {handler_name!r} for retained step "
                        f"{step.tool!r} is not registered in this process; it "
                        f"must import the same connectors the agent did"])
                inherited = SagaStep(
                    tool=step.tool,
                    semantics=ActionSemantics(step.semantics),
                    state=StepState.COMMITTED,
                    step_id=step.step_id,
                    compensation=Compensation(
                        fn=handler, handler=handler_name,
                        kwargs=dict(desc.get("kwargs") or {}),
                        description=desc.get("description")
                        or f"inherited inverse for {step.tool}"),
                )
                ctx.stack.append(inherited)

            ctx.wal.append("REPAIR_RESUMED", {
                "saga_id": ctx.saga_id, "repaired_saga_id": self.saga_id,
                "operator": self.operator,
                "inherited": len(self.retained),
                "amended_args": _safe(self._args),
            })
            return await continuation(ctx)

    def abandon(self, wal: Any) -> dict:
        """Decline to repair. Records the decision; the original saga's
        rollback is the recovery daemon's job, not this session's -- a repair
        tool that could also silently trigger rollbacks would be two
        authorities wearing one name."""
        self._used = True
        payload = {
            "repaired_saga_id": self.saga_id, "operator": self.operator,
            "reason": self.reason, "decision": "abandoned",
            "amendments_discarded": len(self._amendments),
        }
        wal.append("REPAIR_ABANDONED", payload)
        logger.info("repair abandoned for saga %s by %s", self.saga_id, self.operator)
        return payload


def _safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _safe(v) for k, v in value.items()}
    return repr(value)[:256]
