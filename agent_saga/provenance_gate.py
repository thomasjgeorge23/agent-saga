"""Argument provenance: an agent may not move money on a number it made up.

Every other control in this package answers "was this action allowed?" or "can
it be undone?". None of them ask the question that actually distinguishes a
useful agent from a dangerous one:

    where did that argument come from?

`stripe.charge(amount=4200)` passes every gate ever written. It is within
budget, it is COMPENSABLE, it has a refund handler, it is logged and provable.
And if the model hallucinated `4200` — misread a table, averaged two invoices,
transposed a digit — every one of those controls works perfectly while the wrong
amount leaves the building. The rollback is clean. The audit is intact. The
customer is still wrong.

So this gate classifies each argument by where it came from, and refuses the
call when a critical one was invented:

    SOURCED   traceable to a ContextBroker receipt that resolves RIGHT NOW
    USER      verbatim in the human's own request
    DERIVED   computed from sourced values by a declared transform
    MODEL     the model produced it and nothing backs it

**Untagged means MODEL.** Absence of provenance is not evidence of provenance,
and defaulting the other way would make the whole check decorative — the one
argument someone forgot to tag would be the one that sails through.

**SOURCED is verified, never accepted.** A caller cannot simply label a value
sourced: the gate re-hydrates the receipt against the cold store and compares
the value to the span it claims to come from. A receipt that no longer resolves,
or text that does not contain the value, downgrades to MODEL and is refused like
any other invention. That is the difference between provenance and a promise.

    policy = ProvenancePolicy()
    policy.require("stripe.charge", "amount", Provenance.USER)
    policy.require("email.send", "to", Provenance.SOURCED)

    policy.check("stripe.charge", {"amount": tagged_amount}, broker=broker)

`prohibit()` additionally refuses named tools outright, which is how an operator
declares actions this deployment must never take regardless of who asks.

On misuse, stated plainly rather than implied: this is an **operator control,
not a guarantee against a determined misuser**. Anyone who controls the process
can edit the policy or bypass the library. What it does provide is a declared,
auditable boundary — a refusal recorded before the effect, and a log a reviewer
can check afterwards. No library can make a person honest; this one can make
what they did legible.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("agent_saga.provenance_gate")

__all__ = [
    "Provenance",
    "ProvenancePolicy",
    "ProvenanceViolation",
    "Tagged",
    "derived",
    "sourced",
    "user",
]


class Provenance(enum.IntEnum):
    """How well an argument is backed. Ordered, so a policy can demand a floor.

    IntEnum on purpose: `require(..., Provenance.USER)` should accept anything
    at least that well-sourced, and an ordering makes that one comparison
    instead of a lookup table nobody maintains.
    """

    MODEL = 0
    """The model produced it. Possibly right; nothing backs it."""

    DERIVED = 1
    """Computed from sourced values by a transform the caller declared."""

    USER = 2
    """Present verbatim in the human's request."""

    SOURCED = 3
    """Traceable to a document span whose hash still matches."""


class ProvenanceViolation(Exception):
    """Raised BEFORE the side effect. Carries every failing argument, because
    an agent that fixes one and retries should not discover the second on the
    next attempt."""

    def __init__(self, tool: str, failures: Sequence[str]):
        self.tool = tool
        self.failures = list(failures)
        super().__init__(
            f"{tool} refused on argument provenance:\n  - "
            + "\n  - ".join(self.failures))


@dataclass(frozen=True)
class Tagged:
    """A value plus where it came from.

    `receipt` is required for SOURCED and is a `context_broker.Span`; the gate
    resolves it rather than trusting the label.
    """

    value: Any
    provenance: Provenance
    receipt: Any = None
    note: str = ""

    def describe(self) -> dict:
        return {"provenance": self.provenance.name, "note": self.note,
                "receipt": self.receipt.describe() if self.receipt is not None
                           and hasattr(self.receipt, "describe") else None}


def sourced(value: Any, receipt: Any, note: str = "") -> Tagged:
    """Claim a value comes from a document span. The claim is checked."""
    if receipt is None:
        raise ValueError(
            "sourced() requires a receipt (a context_broker.Span). A SOURCED "
            "label with nothing to resolve is just MODEL with better manners.")
    return Tagged(value, Provenance.SOURCED, receipt=receipt, note=note)


def user(value: Any, note: str = "") -> Tagged:
    return Tagged(value, Provenance.USER, note=note)


def derived(value: Any, note: str) -> Tagged:
    """A computed value. The note is mandatory: `DERIVED` without a stated
    derivation is indistinguishable from `MODEL`."""
    if not note.strip():
        raise ValueError(
            "derived() requires a note stating the derivation; without one this "
            "is a model-produced number wearing a better label")
    return Tagged(value, Provenance.DERIVED, note=note)


class ProvenancePolicy:
    """Per-tool, per-argument provenance floors, plus outright prohibitions."""

    def __init__(self) -> None:
        self._floors: Dict[Tuple[str, str], Provenance] = {}
        self._prohibited: Dict[str, str] = {}

    # -- declaration ------------------------------------------------------------

    def require(self, tool: str, argument: str,
               minimum: Provenance) -> "ProvenancePolicy":
        """Refuse `tool` unless `argument` is at least `minimum`."""
        if not isinstance(minimum, Provenance):
            raise TypeError(f"minimum must be a Provenance, got {type(minimum).__name__}")
        if minimum is Provenance.MODEL:
            raise ValueError(
                "requiring MODEL is a floor everything already meets, so it "
                "reads as a control while enforcing nothing. Omit the rule, or "
                "raise the floor.")
        self._floors[(tool, argument)] = minimum
        return self

    def prohibit(self, tool: str, reason: str) -> "ProvenancePolicy":
        """Refuse a tool outright, whoever asks.

        For actions a deployment must never take. An operator control -- see
        this module's docstring on what it does and does not guarantee.
        """
        if not reason.strip():
            raise ValueError("a prohibition needs a reason; the refusal message "
                             "is what the operator reads at 3am")
        self._prohibited[tool] = reason
        return self

    # -- enforcement -------------------------------------------------------------

    def classify(self, value: Any, *, broker: Any = None) -> Provenance:
        """The provenance actually established for a value.

        An untagged value is MODEL. A SOURCED claim is verified against the
        broker and downgraded to MODEL if the receipt does not resolve or the
        span does not contain the value.
        """
        if not isinstance(value, Tagged):
            return Provenance.MODEL
        if value.provenance is not Provenance.SOURCED:
            return value.provenance

        if broker is None:
            logger.warning(
                "a SOURCED argument was presented with no broker to verify it "
                "against; treating it as MODEL rather than taking the label on "
                "trust")
            return Provenance.MODEL
        try:
            text = broker.hydrate(value.receipt)
        except Exception as exc:
            logger.warning("receipt did not resolve (%r); downgrading to MODEL", exc)
            return Provenance.MODEL

        if str(value.value) not in text:
            logger.warning(
                "value %r is not present in the span it claims to come from; "
                "downgrading to MODEL", value.value)
            return Provenance.MODEL
        return Provenance.SOURCED

    def check(self, tool: str, kwargs: Mapping[str, Any], *,
              broker: Any = None) -> None:
        """Raise `ProvenanceViolation` unless every rule for `tool` is met.

        Called before the effect, so refusing is free. Every failing argument is
        reported at once.
        """
        if tool in self._prohibited:
            raise ProvenanceViolation(
                tool, [f"prohibited by policy: {self._prohibited[tool]}"])

        failures: List[str] = []
        for (rule_tool, argument), minimum in sorted(self._floors.items()):
            if rule_tool != tool:
                continue
            if argument not in kwargs:
                failures.append(
                    f"{argument!r} requires provenance {minimum.name} but was "
                    f"not supplied; a rule on a missing argument is a rule that "
                    f"silently stops applying")
                continue
            actual = self.classify(kwargs[argument], broker=broker)
            if actual < minimum:
                failures.append(
                    f"{argument!r} is {actual.name} but {minimum.name} or better "
                    f"is required"
                    + (f" (claimed {kwargs[argument].provenance.name}; the "
                       f"receipt did not check out)"
                       if isinstance(kwargs[argument], Tagged)
                       and kwargs[argument].provenance > actual else ""))

        if failures:
            raise ProvenanceViolation(tool, failures)

    # -- plumbing ---------------------------------------------------------------------

    @staticmethod
    def unwrap(kwargs: Mapping[str, Any]) -> Dict[str, Any]:
        """Strip the tags, for handing the arguments to the real tool."""
        return {k: (v.value if isinstance(v, Tagged) else v)
                for k, v in kwargs.items()}

    def describe(self) -> dict:
        return {
            "requirements": [{"tool": t, "argument": a, "minimum": m.name}
                             for (t, a), m in sorted(self._floors.items())],
            "prohibited": [{"tool": t, "reason": r}
                           for t, r in sorted(self._prohibited.items())],
        }

    def format_text(self) -> str:
        lines = ["argument provenance policy"]
        if not self._floors and not self._prohibited:
            lines.append("  nothing declared -- this policy enforces nothing")
            return "\n".join(lines)
        for (tool, argument), minimum in sorted(self._floors.items()):
            lines.append(f"  {tool}({argument}=...) requires {minimum.name} or better")
        for tool, reason in sorted(self._prohibited.items()):
            lines.append(f"  {tool} PROHIBITED: {reason}")
        return "\n".join(lines)

    def as_gate_rule(self, *, broker: Any = None):
        """Adapt this policy into a callable a `PreFlightGate` can evaluate, so
        provenance is enforced on the same path as budgets and approvals rather
        than as a second thing everyone remembers separately."""

        def rule(ctx: Any) -> None:
            self.check(getattr(ctx, "tool", "unknown"),
                       getattr(ctx, "kwargs", {}) or {}, broker=broker)

        rule.__name__ = "argument_provenance"
        return rule
