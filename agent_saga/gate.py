"""Pre-flight policy gate.

The rollback engine is the demo. This is the contract. A bank does not buy a
post-disaster cleanup script -- it buys a control that refuses to enter an
uncompensable boundary without a human on the hook.
"""

from __future__ import annotations

import enum
import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Sequence, Union

from .semantics import ActionSemantics


class Verdict(enum.Enum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class GateContext:
    tool: str
    semantics: ActionSemantics
    kwargs: dict


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    rule: str
    reason: str


class PreFlightViolation(Exception):
    """Raised before any side effect occurs. The defining property of this
    exception is that nothing has happened yet."""

    def __init__(self, decision: Decision, ctx: GateContext):
        self.decision = decision
        self.ctx = ctx
        super().__init__(f"[{decision.verdict.value}] {ctx.tool}: {decision.reason} (rule: {decision.rule})")


Predicate = Callable[[GateContext], bool]
ApprovalProvider = Callable[[GateContext, "Rule"], Union[bool, Awaitable[bool]]]


@dataclass(frozen=True)
class Rule:
    name: str
    when: Predicate
    verdict: Verdict
    reason: str = ""


def semantics_is(*kinds: ActionSemantics) -> Predicate:
    return lambda ctx: ctx.semantics in kinds


def arg_exceeds(arg: str, threshold: float) -> Predicate:
    """Argument-dependent escalation: `transfer(amount=5)` and
    `transfer(amount=5_000_000)` share a tool name and share nothing else."""

    def _p(ctx: GateContext) -> bool:
        v = ctx.kwargs.get(arg)
        return isinstance(v, (int, float)) and not isinstance(v, bool) and v > threshold

    return _p


DEFAULT_RULES: tuple[Rule, ...] = (
    Rule(
        name="irreversible-requires-human",
        when=semantics_is(ActionSemantics.IRREVERSIBLE),
        verdict=Verdict.REQUIRE_APPROVAL,
        reason="Action cannot be undone or compensated by any automated means.",
    ),
)


class PreFlightGate:
    """Rules are evaluated in order; the first match wins. Order therefore
    encodes precedence -- put your BLOCK rules first."""

    def __init__(
        self,
        rules: Sequence[Rule] = DEFAULT_RULES,
        *,
        approval_provider: Optional[ApprovalProvider] = None,
    ):
        self.rules = list(rules)
        self.approval_provider = approval_provider

    async def evaluate(self, ctx: GateContext) -> Decision:
        for rule in self.rules:
            try:
                matched = rule.when(ctx)
            except Exception as exc:  # a broken predicate must fail closed
                raise PreFlightViolation(
                    Decision(Verdict.BLOCK, rule.name, f"policy predicate raised: {exc!r}"), ctx
                ) from exc
            if not matched:
                continue

            decision = Decision(rule.verdict, rule.name, rule.reason)
            if decision.verdict is Verdict.ALLOW:
                return decision
            if decision.verdict is Verdict.BLOCK:
                raise PreFlightViolation(decision, ctx)

            # REQUIRE_APPROVAL
            if self.approval_provider is None:
                raise PreFlightViolation(
                    Decision(
                        Verdict.BLOCK,
                        rule.name,
                        f"{rule.reason} No approval provider is configured, so it cannot be authorized.",
                    ),
                    ctx,
                )
            granted = self.approval_provider(ctx, rule)
            if inspect.isawaitable(granted):
                granted = await granted
            if not granted:
                raise PreFlightViolation(
                    Decision(Verdict.BLOCK, rule.name, f"{rule.reason} Human approval was denied."), ctx
                )
            return Decision(Verdict.ALLOW, rule.name, "Human approval granted.")

        return Decision(Verdict.ALLOW, "default-allow", "No policy rule matched.")


__all__ = [
    "PreFlightGate",
    "PreFlightViolation",
    "Rule",
    "Verdict",
    "Decision",
    "GateContext",
    "semantics_is",
    "arg_exceeds",
    "DEFAULT_RULES",
]
