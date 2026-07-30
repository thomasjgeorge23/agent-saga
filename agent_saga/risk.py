"""Failure prediction from the log: "this call resembles ones that got undone".

Every other control in this package is deterministic. A budget is over or it is
not; a receipt resolves or it does not; a semantics is declared or the engine
refuses. This module is the one exception, and the exception is confined on
purpose: it **advises**, it does not decide.

The material it works from is the thing only agent-saga has -- a log in which
every action is labelled by whether reality kept it. Fit a model over that and
you can answer, before a call runs, whether it looks like the calls that had to
be undone.

    model = FailureModel.fit(records)
    risk = model.assess("stripe.charge", {"amount": 980000, "currency": "GBP"})
    print(risk.format_text())

Four commitments, each of which cost something:

**It reuses the corpus labels rather than counting rollbacks.** A step rolled
back because step 5 failed was *correct*; counting it as a failure teaches the
model that charging money is dangerous, which is both true and useless. Only
`corpus.Label.REJECTED` counts as a failure, and the attribution logic lives in
one place instead of being reimplemented here subtly differently.

**It reports lift over the base rate, never a raw frequency.** "9 of 12 similar
calls failed" is meaningless if three quarters of everything fails. The number
that carries information is how much *more* often this shape failed than
everything else did.

**Below its support threshold it says so instead of producing a number.** "1 of
1 similar calls failed" is not a 100% risk, and a confident-looking percentage
built on one observation is worse than silence.

**It offers no blocking gate, deliberately.** A correlation is grounds to look,
not grounds to refuse a legitimate action -- and a probabilistic control wired
into a fail-closed boundary would start declining real work for reasons nobody
can audit. `require_review_above()` is provided so a caller can route a
high-lift call to a human, which is the right consequence for a suspicion.

Features are chosen to be explainable rather than powerful: the tool, the shape
of the argument set, and which decile a numeric argument falls in. "This
resembles prior failures because the amount is in the top decile for this tool"
is something an operator can act on. A cosine distance is not.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import (Any, Dict, List, Mapping, Optional, Sequence, Tuple)

logger = logging.getLogger("agent_saga.risk")

__all__ = [
    "FailureModel",
    "RiskAssessment",
    "RiskFactor",
    "require_review_above",
]

DEFAULT_MIN_SUPPORT = 5
"""Fewer observations than this and a rate is noise wearing a percentage."""

_TOP_DECILE = 0.9


@dataclass(frozen=True)
class RiskFactor:
    """One explainable reason this call resembles past failures."""

    feature: str
    observed: int
    failed: int
    base_rate: float

    @property
    def rate(self) -> float:
        return self.failed / self.observed if self.observed else 0.0

    @property
    def lift(self) -> float:
        """How much more often this shape failed than everything else.

        1.0 means "exactly average". A raw rate cannot say that, which is why
        this is the number reported.
        """
        if self.base_rate <= 0:
            return 0.0
        return self.rate / self.base_rate

    def describe(self) -> dict:
        return {"feature": self.feature, "observed": self.observed,
                "failed": self.failed, "rate": round(self.rate, 4),
                "base_rate": round(self.base_rate, 4), "lift": round(self.lift, 3)}


@dataclass(frozen=True)
class RiskAssessment:
    tool: str
    factors: Tuple[RiskFactor, ...]
    base_rate: float
    min_support: int
    total_examples: int

    @property
    def supported(self) -> Tuple[RiskFactor, ...]:
        """Only the factors with enough observations behind them to mean
        anything."""
        return tuple(f for f in self.factors if f.observed >= self.min_support)

    @property
    def sufficient_evidence(self) -> bool:
        return bool(self.supported)

    @property
    def max_lift(self) -> Optional[float]:
        if not self.supported:
            return None
        return max(f.lift for f in self.supported)

    def elevated(self, threshold: float = 2.0) -> bool:
        """Whether any well-supported factor exceeds `threshold` lift.

        Returns False when the evidence is insufficient -- absence of data is
        not absence of risk, and this method says "I cannot tell" by declining
        to claim anything rather than by returning a comforting False that a
        caller would read as "safe".
        """
        lift = self.max_lift
        return lift is not None and lift >= threshold

    def describe(self) -> dict:
        return {"tool": self.tool, "base_rate": round(self.base_rate, 4),
                "total_examples": self.total_examples,
                "sufficient_evidence": self.sufficient_evidence,
                "max_lift": round(self.max_lift, 3) if self.max_lift else None,
                "factors": [f.describe() for f in self.factors]}

    def format_text(self) -> str:
        lines = [f"risk assessment for {self.tool}"]
        if self.total_examples == 0:
            lines.append("  no history: nothing has been logged to learn from")
            return "\n".join(lines)

        lines.append(f"  base failure rate across {self.total_examples} "
                     f"example(s): {self.base_rate:.1%}")
        if not self.sufficient_evidence:
            lines.append("")
            lines.append(f"  INSUFFICIENT EVIDENCE -- no feature of this call has")
            lines.append(f"  {self.min_support}+ prior observations. That is not a")
            lines.append(f"  clean bill of health; it means the log cannot say.")
            for weak in self.factors:
                lines.append(f"    ({weak.observed} observation(s): {weak.feature})")
            return "\n".join(lines)

        for factor in sorted(self.supported, key=lambda f: -f.lift):
            lines.append(f"  x{factor.lift:.1f} lift  {factor.failed}/"
                         f"{factor.observed} failed  {factor.feature}")
        lines.append("")
        lines.append(f"  This is a correlation drawn from past outcomes, not a")
        lines.append(f"  verdict on this call. Route it to a human if the lift")
        lines.append(f"  warrants it; do not let it silently refuse real work.")
        return "\n".join(lines)


class FailureModel:
    """Failure rates per explainable feature, learned from labelled history."""

    def __init__(self, *, min_support: int = DEFAULT_MIN_SUPPORT):
        if min_support < 1:
            raise ValueError(f"min_support must be >= 1, got {min_support}")
        self.min_support = min_support
        self._counts: Dict[str, List[int]] = {}      # feature -> [observed, failed]
        self._deciles: Dict[str, float] = {}         # "tool\x1ffield" -> P90
        self.total = 0
        self.failures = 0

    # -- fitting ------------------------------------------------------------------

    @classmethod
    def fit(cls, records: Sequence[Mapping[str, Any]], *,
            min_support: int = DEFAULT_MIN_SUPPORT) -> "FailureModel":
        """Learn from WAL records, using `corpus`'s labels for attribution.

        `COLLATERAL` and `AMBIGUOUS` examples are excluded from both numerator
        and denominator: a step undone for someone else's failure is not
        evidence about this one, and an UNKNOWN outcome is not evidence at all.
        """
        from .corpus import Label, build_corpus

        model = cls(min_support=min_support)
        corpus = build_corpus(records)

        numeric: Dict[str, List[float]] = {}
        trainable = corpus.trainable
        for example in trainable:
            for name, value in example.arguments.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                numeric.setdefault(f"{example.tool}\x1f{name}", []).append(float(value))

        for key, values in numeric.items():
            model._deciles[key] = _percentile(values, _TOP_DECILE)

        for example in trainable:
            failed = example.label is Label.REJECTED
            model.total += 1
            model.failures += int(failed)
            for feature in model._features(example.tool, example.arguments):
                slot = model._counts.setdefault(feature, [0, 0])
                slot[0] += 1
                slot[1] += int(failed)
        return model

    # -- assessment ------------------------------------------------------------------

    @property
    def base_rate(self) -> float:
        return self.failures / self.total if self.total else 0.0

    def assess(self, tool: str, arguments: Mapping[str, Any]) -> RiskAssessment:
        """How much this call resembles the ones that had to be undone."""
        factors: List[RiskFactor] = []
        for feature in self._features(tool, arguments):
            observed, failed = self._counts.get(feature, (0, 0))
            if observed:
                factors.append(RiskFactor(feature=feature, observed=observed,
                                          failed=failed, base_rate=self.base_rate))
        return RiskAssessment(tool=tool, factors=tuple(factors),
                              base_rate=self.base_rate,
                              min_support=self.min_support,
                              total_examples=self.total)

    def describe(self) -> dict:
        return {"examples": self.total, "failures": self.failures,
                "base_rate": round(self.base_rate, 4),
                "features": len(self._counts), "min_support": self.min_support}

    def format_text(self) -> str:
        lines = [f"failure model: {self.total} labelled example(s), "
                 f"{self.failures} failure(s)"]
        if not self.total:
            lines.append("  nothing trainable in this log -- the model predicts nothing")
            return "\n".join(lines)
        lines.append(f"  base rate : {self.base_rate:.1%}")
        lines.append(f"  features  : {len(self._counts)}")
        lines.append(f"  support   : {self.min_support} observation(s) minimum")
        return "\n".join(lines)

    # -- features -----------------------------------------------------------------------

    def _features(self, tool: str, arguments: Mapping[str, Any]) -> List[str]:
        """Explainable features only.

        An operator has to be able to act on the answer, so each feature reads
        as a sentence: which tool, which argument set, which magnitude band. A
        learned embedding might separate failures better and would tell nobody
        anything they could use.
        """
        out = [f"tool={tool}"]

        shape = ",".join(sorted(str(k) for k in arguments))
        out.append(f"tool={tool} args=[{shape}]")

        for name, value in sorted(arguments.items()):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            cutoff = self._deciles.get(f"{tool}\x1f{name}")
            if cutoff is not None and float(value) >= cutoff:
                out.append(f"tool={tool} {name} in top decile (>= {cutoff:g})")
        return out


def require_review_above(model: FailureModel, threshold: float = 2.0):
    """Build a callable that routes a high-lift call to human review.

    Returned as an *advisory* hook rather than a gate rule: it raises
    `PreFlightViolation` so the surrounding engine treats it like any other
    refusal, but wiring it in is an explicit decision the operator makes, and
    the message says the refusal came from a correlation. A statistical control
    installed silently into a fail-closed boundary would start declining real
    work for reasons nobody can audit.
    """
    from .gate import Decision, PreFlightViolation, Verdict

    def rule(ctx: Any) -> None:
        tool = getattr(ctx, "tool", "unknown")
        assessment = model.assess(tool, getattr(ctx, "kwargs", {}) or {})
        if not assessment.elevated(threshold):
            return
        # REQUIRE_APPROVAL rather than BLOCK: the finding is a resemblance, so
        # the correct consequence is a human looking, not a refusal.
        raise PreFlightViolation(
            Decision(
                verdict=Verdict.REQUIRE_APPROVAL,
                rule=rule.__name__,
                reason=(f"resembles calls that were undone "
                        f"(x{assessment.max_lift:.1f} the base failure rate). "
                        f"This is a correlation from history, not a defect found "
                        f"in this call -- it is raised so a human decides."),
            ),
            ctx,
        )

    rule.__name__ = f"failure_risk_above_{threshold:g}x"
    return rule


def _percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile. No numpy, and no interpolation subtleties to
    argue about in a threshold nobody should be tuning finely."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]
