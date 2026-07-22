"""Reconciliation: check that what the log claims actually happened.

Every other guarantee in this library ends at an API response. The refund
returned 200, so the WAL says COMPENSATED, so the rollback report says clean. A
bank does not accept that chain, and it is right not to: a 200 is an
acknowledgement, not a fact about the ledger. It can be returned by an
idempotency key that matched a *different* operation, by a write that was later
voided, by a call that reached the wrong tenant, or by a queue that accepted the
work and then dropped it.

So this pass ignores what the log says happened and asks the external system
what is true. Three outcomes, and the third is the one most tools get wrong:

  * CONFIRMED    -- the system agrees with the log.
  * DRIFT        -- it does not. The whole reason this exists.
  * UNVERIFIABLE -- nobody could tell us. Never counted as confirmed, never
                    hidden in a total. A reconciliation report that quietly
                    treats "could not check" as "fine" is worse than no report,
                    because it is the one an auditor will be shown.

It also resolves the hardest state in the engine. A step that timed out is
recorded UNKNOWN, because a timed-out POST to Stripe may well have charged the
card, and no amount of in-process reasoning can settle it. Asking the card
network is the *only* way to find out, and it is what a human would otherwise do
by hand, at 3am, against a spreadsheet.

Deliberately a separate, later pass rather than an inline check. Payment and CRM
APIs are eventually consistent; reading back immediately after a write would
report drift that is merely latency, and a control that cries wolf gets muted.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger("agent_saga.reconcile")

# What we believe the external world should look like.
REVERSED = "REVERSED"
"""The effect was undone; the compensation should be visible."""
PRESENT = "PRESENT"
"""The effect stands -- committed and not rolled back, or irreversible."""
INDETERMINATE = "INDETERMINATE"
"""We do not know (a timed-out step). The observation decides."""

# What the external system told us.
CONFIRMED = "CONFIRMED"
DRIFT = "DRIFT"
UNVERIFIABLE = "UNVERIFIABLE"

_RECONCILERS: dict = {}


def reconciler(handler: str) -> Callable:
    """Register an observer for a compensation handler.

    Keyed on the same name the compensation registers under, so the pair is
    obvious at the call site: whoever writes `@compensator("stripe.refund")`
    can see whether anyone ever wrote the check for it.

        @reconciler("stripe.refund")
        async def observe_refund(*, charge_id, **kwargs):
            charge = await stripe.Charge.retrieve(charge_id)
            return Observation(reversed_=charge.refunded, detail=charge.status)
    """

    def _register(fn: Callable) -> Callable:
        if handler in _RECONCILERS:
            logger.warning("reconciler for %r replaced", handler)
        _RECONCILERS[handler] = fn
        return fn

    return _register


def registered_reconcilers() -> dict:
    return dict(_RECONCILERS)


def clear_reconcilers() -> None:
    _RECONCILERS.clear()


@dataclass(frozen=True)
class Observation:
    """What the external system reports.

    `reversed_` is a tri-state on purpose: True (the inverse is visible), False
    (the original effect still stands), or None (the system could not tell us).
    A boolean would force None into one of the other two, and whichever we chose
    would be a lie in some real case.
    """

    reversed_: Optional[bool] = None
    exists: Optional[bool] = None
    detail: str = ""
    amount: Optional[float] = None

    @property
    def unknown(self) -> bool:
        return self.reversed_ is None and self.exists is None


@dataclass
class Finding:
    saga_id: str
    step_id: str
    tool: str
    handler: str
    expected: str
    outcome: str
    detail: str = ""
    kwargs: dict = field(default_factory=dict)

    @property
    def is_drift(self) -> bool:
        return self.outcome == DRIFT

    def __str__(self) -> str:
        return (f"[{self.outcome}] {self.tool} (saga {self.saga_id[:12]}, "
                f"step {self.step_id[:12]}): expected {self.expected}"
                + (f" -- {self.detail}" if self.detail else ""))


@dataclass
class ReconcileReport:
    confirmed: list = field(default_factory=list)
    drift: list = field(default_factory=list)
    unverifiable: list = field(default_factory=list)
    checked: int = 0
    started_at: float = field(default_factory=time.time)

    @property
    def clean(self) -> bool:
        """Unverifiable findings count against clean.

        A report that says "all good" while a third of the estate could not be
        checked is precisely the report that gets shown to an auditor and
        precisely the one that is wrong.
        """
        return not self.drift and not self.unverifiable

    def summary(self) -> str:
        if self.checked == 0:
            return "nothing to reconcile"
        parts = [f"{len(self.confirmed)} confirmed"]
        if self.drift:
            parts.append(f"{len(self.drift)} DRIFT")
        if self.unverifiable:
            parts.append(f"{len(self.unverifiable)} unverifiable")
        head = "reconciled" if self.clean else "RECONCILIATION FOUND PROBLEMS"
        return f"{head}: {self.checked} effect(s) -- " + ", ".join(parts)


# ---------------------------------------------------------------------------
# Deriving expectations from the log
# ---------------------------------------------------------------------------

@dataclass
class _Effect:
    saga_id: str
    step_id: str
    tool: str
    handler: str = ""
    kwargs: dict = field(default_factory=dict)
    expected: str = PRESENT
    recoverable: bool = True


def expectations(records: Iterable[dict]) -> list:
    """Read the WAL and say what the world should look like.

    The terminal record for a step wins, which is why this is a fold rather than
    a filter: a step that was COMMITTED and later COMPENSATED must be expected
    REVERSED, and the naive "collect every STEP_COMMITTED" reading would assert
    the opposite of the truth on every rolled-back saga.
    """
    effects: dict = {}

    def touch(record: dict) -> Optional[_Effect]:
        saga_id, step_id = record.get("saga_id"), record.get("step_id")
        if not saga_id or not step_id:
            return None
        key = (saga_id, step_id)
        effect = effects.get(key)
        if effect is None:
            effect = _Effect(saga_id=saga_id, step_id=step_id,
                             tool=record.get("tool", "?"))
            effects[key] = effect
        return effect

    for record in records:
        event = record.get("event")
        if event not in ("STEP_COMMITTED", "STEP_UNKNOWN", "COMPENSATED",
                         "COMPENSATION_FAILED", "STEP_ORPHANED"):
            continue
        effect = touch(record)
        if effect is None:
            continue
        if record.get("tool"):
            effect.tool = record["tool"]

        comp = record.get("compensation")
        if isinstance(comp, dict):
            effect.handler = comp.get("handler") or effect.handler
            effect.kwargs = comp.get("kwargs") or effect.kwargs
            effect.recoverable = bool(comp.get("recoverable", True))

        if event == "STEP_COMMITTED":
            effect.expected = PRESENT
        elif event == "STEP_UNKNOWN":
            # The step raised. It may or may not have landed, and only the
            # external system can say which.
            effect.expected = INDETERMINATE
        elif event == "COMPENSATED":
            effect.expected = REVERSED
        elif event in ("COMPENSATION_FAILED", "STEP_ORPHANED"):
            # The undo did not happen. The original effect still stands, and
            # asserting that is how a silently-failed rollback gets caught.
            effect.expected = PRESENT

    return list(effects.values())


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

class Reconciliation:
    """Compare the log against the systems it claims to have changed."""

    def __init__(self, wal: Any = None, *, timeout: float = 30.0,
                 concurrency: int = 8):
        self.wal = wal
        self.timeout = timeout
        self.concurrency = concurrency

    async def run(self, records: Iterable[dict]) -> ReconcileReport:
        report = ReconcileReport()
        effects = [e for e in expectations(records) if e.handler]
        semaphore = asyncio.Semaphore(self.concurrency)

        async def check(effect: _Effect) -> Finding:
            async with semaphore:
                return await self._check(effect)

        findings = await asyncio.gather(*(check(e) for e in effects))
        for finding in findings:
            report.checked += 1
            if finding.outcome == CONFIRMED:
                report.confirmed.append(finding)
            elif finding.outcome == DRIFT:
                report.drift.append(finding)
            else:
                report.unverifiable.append(finding)
            self._record(finding)

        (logger.info if report.clean else logger.error)(report.summary())
        return report

    async def _check(self, effect: _Effect) -> Finding:
        observer = _RECONCILERS.get(effect.handler)
        if observer is None:
            return Finding(
                effect.saga_id, effect.step_id, effect.tool, effect.handler,
                effect.expected, UNVERIFIABLE, kwargs=effect.kwargs,
                detail=(f"no reconciler registered for {effect.handler!r}; "
                        f"this effect is asserted by the log alone"))
        try:
            result = observer(**effect.kwargs)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, self.timeout)
        except asyncio.TimeoutError:
            return Finding(effect.saga_id, effect.step_id, effect.tool,
                           effect.handler, effect.expected, UNVERIFIABLE,
                           kwargs=effect.kwargs,
                           detail=f"observation timed out after {self.timeout}s")
        except Exception as exc:
            return Finding(effect.saga_id, effect.step_id, effect.tool,
                           effect.handler, effect.expected, UNVERIFIABLE,
                           kwargs=effect.kwargs,
                           detail=f"observation failed: {exc!r}")

        if not isinstance(result, Observation):
            return Finding(effect.saga_id, effect.step_id, effect.tool,
                           effect.handler, effect.expected, UNVERIFIABLE,
                           kwargs=effect.kwargs,
                           detail=(f"reconciler returned {type(result).__name__}, "
                                   f"expected Observation"))
        return self._compare(effect, result)

    def _compare(self, effect: _Effect, obs: Observation) -> Finding:
        def finding(outcome: str, detail: str) -> Finding:
            return Finding(effect.saga_id, effect.step_id, effect.tool,
                           effect.handler, effect.expected, outcome, detail,
                           dict(effect.kwargs))

        if obs.unknown:
            return finding(UNVERIFIABLE,
                           f"system could not determine the state{_suffix(obs)}")

        if effect.expected == REVERSED:
            if obs.reversed_ is True:
                return finding(CONFIRMED, f"reversal is visible{_suffix(obs)}")
            return finding(
                DRIFT,
                f"the log says this was compensated, but the system still shows "
                f"the original effect standing{_suffix(obs)}")

        if effect.expected == PRESENT:
            if obs.reversed_ is True:
                return finding(
                    DRIFT,
                    f"the system shows this reversed, but the log has no "
                    f"successful compensation for it{_suffix(obs)}")
            if obs.exists is False:
                return finding(
                    DRIFT,
                    f"the log records this effect as having happened, but the "
                    f"system has no record of it{_suffix(obs)}")
            return finding(CONFIRMED, f"effect stands as recorded{_suffix(obs)}")

        # INDETERMINATE: the step timed out. Any definite answer is progress.
        if obs.exists is False and obs.reversed_ is not True:
            return finding(CONFIRMED,
                           f"timed-out step did NOT land; nothing to undo{_suffix(obs)}")
        if obs.reversed_ is True:
            return finding(CONFIRMED,
                           f"timed-out step landed and was reversed{_suffix(obs)}")
        return finding(
            DRIFT,
            f"timed-out step DID land and is still standing -- it was never "
            f"compensated{_suffix(obs)}")

    def _record(self, finding: Finding) -> None:
        if self.wal is None:
            return
        try:
            self.wal.append(f"RECONCILE_{finding.outcome}", {
                "saga_id": finding.saga_id, "step_id": finding.step_id,
                "tool": finding.tool, "handler": finding.handler,
                "expected": finding.expected, "detail": finding.detail})
        except Exception as exc:
            logger.error("could not record reconciliation finding: %r", exc)


def _suffix(obs: Observation) -> str:
    bits = []
    if obs.detail:
        bits.append(obs.detail)
    if obs.amount is not None:
        bits.append(f"amount={obs.amount:g}")
    return f" ({'; '.join(bits)})" if bits else ""


__all__ = [
    "reconciler", "registered_reconcilers", "clear_reconcilers",
    "Observation", "Finding", "ReconcileReport", "Reconciliation",
    "expectations", "REVERSED", "PRESENT", "INDETERMINATE",
    "CONFIRMED", "DRIFT", "UNVERIFIABLE",
]
