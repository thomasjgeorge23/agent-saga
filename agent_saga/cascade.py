"""Verification-gated cascade: the cheapest host that can be *proven* right.

No middleware makes a model smarter. What it can do is stop asking a weak model
to be trusted, and start asking it to be *checked* -- then escalate on the
evidence when the check fails.

    result = await cascade(request, ladder=[local_7b, mid_tier, frontier],
                           verify=must_be_valid_json_matching(Schema))

The 7B answers first. Its output is verified against something that can
actually fail -- a schema, a grounding check, the tool registry, an entailment
judge. If it passes, you paid 7B prices for a verified answer. If it fails, the
next host up gets the request **plus the specific reason the last one was
rejected**, which is the part that measurably helps rather than just costing
more.

What this does and does not claim:

  * **It does not claim the weak model improved.** It claims the answer you got
    was verified, and names which tier produced it. That is a different and
    more useful guarantee than "our layer makes any LLM frontier-grade", which
    is not a thing anyone can deliver.
  * **Escalation is evidence-driven, not confidence-guessed.** Asking a model
    how sure it is produces a number weakly correlated with being right; a
    verifier that reruns the schema, resolves the citation, or checks the tool
    exists produces a fact. Only facts escalate here.
  * **The saving is measured, not asserted.** Every rung records its host,
    verdict, rejection reason, tokens, and wall clock, so
    `result.describe()` shows what each tier actually cost and how often the
    cheap one was enough. If the cascade is not saving you anything, the report
    is where you find that out.

Relationship to `router.complete()`: that deliberately refuses to fall back to
another host when validation fails, because retrying an unacceptable answer on
a *cheaper* host is routing on hope. This module goes the other way -- up the
ladder, toward more capability, carrying the reason -- which is principled for
exactly the reason the downward move is not.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import (Any, Callable, List, Optional, Sequence, Tuple)

from .ir import ChatRequest, ChatResponse, Message

logger = logging.getLogger("agent_saga.cascade")

__all__ = [
    "CascadeExhausted",
    "CascadeResult",
    "Rung",
    "cascade",
    "tools_must_exist",
]

_ESCALATION = (
    "A previous attempt at this task was rejected by an automated check:\n"
    "{reason}\n"
    "Produce a corrected response that satisfies that check exactly."
)


class CascadeExhausted(RuntimeError):
    """Every host on the ladder produced output that failed verification.

    Carries the full trace. An unverified answer is never returned as though it
    had passed -- that would make the whole cascade decorative.
    """

    def __init__(self, rungs: Sequence["Rung"]):
        self.rungs = tuple(rungs)
        detail = "\n".join(f"  {r.host}: {r.reason}" for r in self.rungs)
        super().__init__(
            f"no host on the ladder produced a verified answer:\n{detail}")


@dataclass(frozen=True)
class Rung:
    """One host's attempt, and what happened to it."""

    host: str
    accepted: bool
    attempts: int
    tokens: int
    elapsed_s: float
    reason: Optional[str] = None          # why it was rejected, if it was

    def describe(self) -> dict:
        return {"host": self.host, "accepted": self.accepted,
                "attempts": self.attempts, "tokens": self.tokens,
                "elapsed_s": round(self.elapsed_s, 3), "reason": self.reason}


@dataclass(frozen=True)
class CascadeResult:
    response: ChatResponse
    rungs: Tuple[Rung, ...]

    @property
    def host(self) -> str:
        """The tier that produced the verified answer."""
        return self.response.provider

    @property
    def escalations(self) -> int:
        return sum(1 for r in self.rungs if not r.accepted)

    @property
    def tokens_used(self) -> int:
        """Across every tier, including the ones that were rejected. Counting
        only the winner would flatter the cascade."""
        return sum(r.tokens for r in self.rungs)

    @property
    def resolved_at_first_tier(self) -> bool:
        return self.escalations == 0

    def describe(self) -> dict:
        return {"host": self.host, "escalations": self.escalations,
                "tokens_used": self.tokens_used,
                "resolved_at_first_tier": self.resolved_at_first_tier,
                "rungs": [r.describe() for r in self.rungs]}

    def format_text(self) -> str:
        lines = [f"cascade resolved on {self.host} "
                 f"after {self.escalations} escalation(s)"]
        for rung in self.rungs:
            mark = "  ok  " if rung.accepted else "  ->  "
            lines.append(f"{mark}{rung.host:<16} {rung.tokens:>6} tok  "
                         f"{rung.elapsed_s:.2f}s"
                         + (f"  rejected: {rung.reason}" if rung.reason else ""))
        lines.append(f"  total tokens across all tiers: {self.tokens_used}")
        return "\n".join(lines)


async def cascade(
    request: ChatRequest,
    *,
    ladder: Sequence[Any],
    verify: Callable[[ChatResponse], Any],
    ctx: Any = None,
    repair_rounds: int = 1,
    attempt_timeout: Optional[float] = None,
) -> CascadeResult:
    """Try each host in `ladder` order until one produces a verified answer.

    `ladder` is ordered cheapest-first; each entry is an `ir.HostAdapter`.
    `verify(response)` raises to reject -- the same convention
    `router.complete`'s validator uses, so a schema check, a grounding check, or
    an LLM judge can be dropped in unchanged.

    `repair_rounds` re-asks the *same* host with the rejection reason before
    escalating, because a weak model given the precise failure often fixes it,
    and that is far cheaper than a frontier call.

    Raises `CascadeExhausted` if nothing verifies. When `ctx` is a saga context,
    the whole ladder trace lands in its WAL as `CASCADE_RESOLVED` -- including
    the failures, which are the interesting part.
    """
    if not ladder:
        raise ValueError("a cascade needs at least one host in its ladder")

    rungs: List[Rung] = []
    current = request
    last_reason: Optional[str] = None

    try:
        for adapter in ladder:
            name = getattr(adapter, "name", type(adapter).__name__)
            started = time.monotonic()
            tokens = 0
            attempts = 0
            accepted = False
            reason: Optional[str] = None
            response: Optional[ChatResponse] = None

            # Same-host repair first: cheapest possible correction.
            for attempt in range(repair_rounds + 1):
                attempts = attempt + 1
                try:
                    response = await _call(adapter, current, attempt_timeout)
                except Exception as exc:
                    reason = f"host raised: {exc!r}"
                    break

                tokens += _tokens(current, response)
                try:
                    verify(response)
                except Exception as exc:
                    reason = str(exc) or repr(exc)
                    if attempt < repair_rounds:
                        current = _with_feedback(current, response, reason)
                        continue
                    break
                accepted = True
                reason = None
                break

            rungs.append(Rung(host=name, accepted=accepted, attempts=attempts,
                              tokens=tokens,
                              elapsed_s=time.monotonic() - started, reason=reason))

            if accepted and response is not None:
                return CascadeResult(response=response, rungs=tuple(rungs))

            # Escalate, carrying the concrete reason the last tier was rejected.
            last_reason = reason
            current = _with_feedback(request, None, reason or "unspecified")
            logger.info("cascade: %s rejected (%s); escalating", name, reason)

        raise CascadeExhausted(rungs)
    finally:
        if ctx is not None:
            try:
                ctx.wal.append("CASCADE_RESOLVED", {
                    "saga_id": getattr(ctx, "saga_id", None),
                    "resolved_host": next((r.host for r in rungs if r.accepted), None),
                    "escalations": sum(1 for r in rungs if not r.accepted),
                    "tokens_used": sum(r.tokens for r in rungs),
                    "rungs": [r.describe() for r in rungs],
                })
            except Exception:               # our bookkeeping, never their call
                logger.exception("cascade: failed to record the ladder trace")


# -- a verifier worth having built in ---------------------------------------------

def tools_must_exist(known: Sequence[str]) -> Callable[[ChatResponse], None]:
    """Reject a response that calls a tool the system does not have.

    The single most common weak-model failure in agent loops, and one a check
    can settle absolutely -- unlike "is this answer good?", "does this tool
    exist?" has one right answer. Catching it before execution turns a runtime
    crash into an escalation.
    """
    available = set(known)

    def verify(response: ChatResponse) -> None:
        unknown = [c.name for c in response.tool_calls if c.name not in available]
        if unknown:
            raise ValueError(
                f"called tool(s) that do not exist: {unknown}. "
                f"Available: {sorted(available)}")

    return verify


# -- internals ----------------------------------------------------------------------

async def _call(adapter: Any, request: ChatRequest,
                timeout: Optional[float]) -> ChatResponse:
    import asyncio

    if timeout is None:
        return await adapter.complete(request)
    return await asyncio.wait_for(adapter.complete(request), timeout)


def _tokens(request: ChatRequest, response: ChatResponse) -> int:
    """Provider-reported usage where available, the package's uniform chars/4
    estimate otherwise. Which one was used is not hidden: a provider reporting
    zero yields the estimate, and the estimate is documented as one."""
    reported = response.usage.input_tokens + response.usage.output_tokens
    if reported > 0:
        return reported
    return request.approx_tokens() + max(1, len(response.text) // 4)


def _with_feedback(request: ChatRequest, response: Optional[ChatResponse],
                   reason: str) -> ChatRequest:
    """Rebuild the request carrying the rejection. The reason is the payload:
    a retry that does not say what was wrong is just a second roll of the same
    dice."""
    messages = list(request.messages)
    if response is not None and response.text:
        messages.append(Message(role="assistant", content=response.text))
    messages.append(Message(role="user", content=_ESCALATION.format(reason=reason)))
    return ChatRequest(
        messages=tuple(messages), tools=request.tools,
        json_only=request.json_only, max_tokens=request.max_tokens,
        temperature=request.temperature, provider_extra=request.provider_extra)
