"""Pre-flight policy gate.

The rollback engine is the demo. This is the contract. A bank does not buy a
post-disaster cleanup script -- it buys a control that refuses to enter an
uncompensable boundary without a human on the hook.
"""

from __future__ import annotations

import enum
import inspect
import logging
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Optional, Sequence, Union

from .semantics import ActionSemantics

logger = logging.getLogger("agent_saga")


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
    `transfer(amount=5_000_000)` share a tool name and share nothing else.

    This sees exactly one call. It cannot express "no more than $50k a day" --
    an agent issuing 1,000 charges of $999 satisfies a $1,000 threshold on every
    one of them. For that, give the gate a `BudgetLimit` (see limits.py).
    """

    def _p(ctx: GateContext) -> bool:
        v = ctx.kwargs.get(arg)
        return isinstance(v, (int, float)) and not isinstance(v, bool) and v > threshold

    return _p


def tool_is(*names: str) -> Predicate:
    """Match specific tools by name."""
    wanted = frozenset(names)
    return lambda ctx: ctx.tool in wanted


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
    encodes precedence -- put your BLOCK rules first.

    `rules` are pure predicates over a single call. `limits` are stateful and
    windowed -- cumulative spend and call velocity -- and are checked *first*,
    so a call already over budget is refused without spending a human's
    attention on approving something that cannot proceed anyway.

    A limit is debited before the rules run and handed back if any rule then
    refuses, because a refusal is the one outcome where we know with certainty
    that the effect did not happen. Once this method returns ALLOW the debit is
    permanent -- see limits.py for why a compensated charge does not earn its
    budget back.
    """

    def __init__(
        self,
        rules: Sequence[Rule] = DEFAULT_RULES,
        *,
        approval_provider: Optional[ApprovalProvider] = None,
        limits: Sequence[Any] = (),
    ):
        self.rules = list(rules)
        self.approval_provider = approval_provider
        self.limits = list(limits)

    async def evaluate(self, ctx: GateContext) -> Decision:
        # Phase 0: is this system allowed to do anything at all? First, so a
        # halted system does not spend budget deciding to refuse, and does not
        # wake a human to approve a call it will refuse regardless.
        self._check_kill_switch(ctx)

        # Phase 0.5: is this call worth making? Before limits, so a call to a
        # dependency known to be down does not consume budget on its way to
        # being refused.
        self._check_breaker(ctx)

        reservation = None
        if self.limits:
            reservation = await self._reserve(ctx)
        try:
            return await self._evaluate_rules(ctx)
        except BaseException:
            # A rule refused, an approval was denied, or a predicate blew up.
            # Nothing executed, so the budget was never really spent -- give it
            # back rather than let refusals silently exhaust the allowance.
            if reservation is not None:
                await self._release(reservation)
            raise

    # -- kill switch -------------------------------------------------------

    def _check_kill_switch(self, ctx: GateContext) -> None:
        from .killswitch import Halted, get_kill_switch
        from .observability import current_correlation

        switch = get_kill_switch()
        if switch is None:
            return
        saga_id, _ = current_correlation()
        try:
            switch.check_step(tool=ctx.tool, saga_id=saga_id or "")
        except Halted as exc:
            raise PreFlightViolation(
                Decision(Verdict.BLOCK, f"kill-switch:{exc.scope}", str(exc)),
                ctx) from exc

    def _check_breaker(self, ctx: GateContext) -> None:
        from .breaker import CircuitOpen, get_breaker

        breaker = get_breaker()
        if breaker is None:
            return
        try:
            breaker.check(ctx.tool)
        except CircuitOpen as exc:
            raise PreFlightViolation(
                Decision(Verdict.BLOCK, f"circuit-open:{ctx.tool}", str(exc)),
                ctx) from exc

    # -- limits ------------------------------------------------------------

    async def _release(self, reservation: Any) -> None:
        from .limits import get_limit_store

        try:
            released = get_limit_store().release(reservation)
            if inspect.isawaitable(released):
                await released
        except Exception as exc:
            # Losing a release over-counts the window, which errs toward
            # refusing later calls. That is the safe direction, so it is logged
            # rather than raised -- it must not mask the refusal underneath.
            logger.warning("could not release limit reservation: %r", exc)

    async def _reserve(self, ctx: GateContext) -> Any:
        """Debit every limit that polices this call, atomically.

        Fails closed throughout: a misconfigured limit, an unreachable store, or
        an exhausted budget with nobody to approve it all BLOCK. A limiter that
        passes calls through when its backend is down is not a limiter.
        """
        from .limits import LimitExceeded, LimitMisconfigured, get_limit_store, plan

        try:
            entries = plan(self.limits, ctx)
        except LimitMisconfigured as exc:
            raise PreFlightViolation(
                Decision(Verdict.BLOCK, "limit-misconfigured", str(exc)), ctx) from exc
        if not entries:
            return None

        store = get_limit_store()
        by_name = {limit.name: limit for limit, _ in entries}
        requests = [req for _, req in entries]
        approved: set[str] = set()

        # Each iteration either succeeds or resolves exactly one exceeded limit
        # via human approval, so this cannot spin more than once per limit.
        for _ in range(len(requests) + 1):
            try:
                outcome = store.reserve(requests)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
            except Exception as exc:
                raise PreFlightViolation(
                    Decision(Verdict.BLOCK, "limit-store-unavailable",
                             f"cannot verify spend limits ({exc!r}), so the call "
                             f"is refused rather than allowed unmetered"), ctx) from exc

            if not isinstance(outcome, LimitExceeded):
                return outcome

            limit = by_name[outcome.limit_name]
            reason = outcome.describe(limit._unit())

            if not limit.escalate_to_human or outcome.limit_name in approved:
                raise PreFlightViolation(
                    Decision(Verdict.BLOCK, f"limit:{outcome.limit_name}", reason), ctx)

            rule = Rule(name=f"limit:{outcome.limit_name}",
                        when=lambda _c: True,
                        verdict=Verdict.REQUIRE_APPROVAL, reason=reason)
            if self.approval_provider is None:
                raise PreFlightViolation(
                    Decision(Verdict.BLOCK, rule.name,
                             f"{reason}. No approval provider is configured, so "
                             f"the overage cannot be authorized."), ctx)
            granted = self.approval_provider(ctx, rule)
            if inspect.isawaitable(granted):
                granted = await granted
            if not granted:
                raise PreFlightViolation(
                    Decision(Verdict.BLOCK, rule.name,
                             f"{reason}. Human approval was denied."), ctx)

            # Authorized overage: retry with this one limit uncapped, so the
            # spend is still *recorded* against the window. Skipping the record
            # would make the next call look like it had fresh budget.
            approved.add(outcome.limit_name)
            requests = [
                replace(req, cap=float("inf")) if req.limit_name in approved else req
                for req in requests
            ]

        raise PreFlightViolation(  # pragma: no cover - loop bound makes this unreachable
            Decision(Verdict.BLOCK, "limit-unresolved",
                     "spend limits could not be resolved"), ctx)

    # -- rules -------------------------------------------------------------

    async def _evaluate_rules(self, ctx: GateContext) -> Decision:
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


class DynamicRiskEvaluator:
    """Evaluates real-time anomaly risk scores for candidate tool calls.

    If an anomaly score exceeds `risk_threshold`, the evaluator dynamically lowers
    the maximum allowed spending threshold or escalates the action to REQUIRE_APPROVAL.
    """

    def __init__(self, risk_scorer: Callable[[GateContext], float], risk_threshold: float = 0.70):
        self.risk_scorer = risk_scorer
        self.risk_threshold = risk_threshold

    def evaluate(self, ctx: GateContext) -> tuple[float, bool]:
        """Returns (risk_score, is_high_risk)."""
        try:
            score = float(self.risk_scorer(ctx))
        except Exception as exc:
            logger.warning("Risk scorer raised exception: %r; defaulting to high risk (1.0)", exc)
            score = 1.0
        return score, score >= self.risk_threshold


def dynamic_risk_rule(name: str, risk_evaluator: DynamicRiskEvaluator, reason: str = "Dynamic AI anomaly risk threshold exceeded") -> Rule:
    """Creates a rule that triggers REQUIRE_APPROVAL when dynamic risk score is high."""
    def _predicate(ctx: GateContext) -> bool:
        score, high_risk = risk_evaluator.evaluate(ctx)
        return high_risk

    return Rule(name=name, when=_predicate, verdict=Verdict.REQUIRE_APPROVAL, reason=reason)


_DEFAULT_GATE: Optional[PreFlightGate] = None


def get_gate() -> PreFlightGate:
    global _DEFAULT_GATE
    if _DEFAULT_GATE is None:
        _DEFAULT_GATE = PreFlightGate()
    return _DEFAULT_GATE


def set_gate(gate: PreFlightGate) -> None:
    global _DEFAULT_GATE
    _DEFAULT_GATE = gate


__all__ = [
    "PreFlightGate",
    "PreFlightViolation",
    "Rule",
    "Verdict",
    "Decision",
    "GateContext",
    "semantics_is",
    "arg_exceeds",
    "tool_is",
    "DEFAULT_RULES",
    "DynamicRiskEvaluator",
    "dynamic_risk_rule",
    "get_gate",
    "set_gate",
]

