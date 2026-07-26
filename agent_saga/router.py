"""Capability-descriptor routing across LLM hosts, with truthful failure.

The `universal` engine's report contract, applied to routing: a router that
answers "no host available" without saying *why each host was disqualified*
is a safety layer overstating its own diagnosis. Every decision here -- and
every refusal -- carries the complete per-host rejection map, and the same
map is what lands in the WAL when a saga context is supplied.

What the router does NOT do:

  * It does not improve any host's output. Routing puts a request on the
    cheapest host whose *declared* capabilities satisfy it; the answer is
    whatever that host produces.
  * It does not silently coerce invalid output. The optional validate/repair
    loop is bounded (default: ONE repair round), and when the rounds are
    exhausted it raises with every validation error it saw, in order.
  * It does not guess about health. Breaker state comes from `breaker.py`;
    an open circuit is a named rejection ("circuit open"), never a silent
    skip, and runtime failures during fallback are recorded per host.

Determinism: given the same request, policy, adapter set, and breaker state,
`route()` returns the same decision. Fallback order is the policy's preference
order -- there is no load-balancing randomness to un-replay.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import (Any, Awaitable, Callable, Dict, List, Mapping, Optional,
                    Sequence, Tuple)

from .breaker import get_breaker
from .ir import Capabilities, ChatRequest, ChatResponse, HostAdapter, Message

logger = logging.getLogger("agent_saga.router")

DEFAULT_ATTEMPT_TIMEOUT = 120.0
"""Seconds one host gets to answer one call. The same argument as the WAL's
barrier timeout: an unbounded wait is a silent failure -- a hung provider must
surface as a named, fallback-able error, never as an agent frozen forever."""

#: A requirement inspects (request, capabilities) and returns None when the
#: host qualifies, or a human-readable reason string when it does not.
Requirement = Callable[[ChatRequest, Capabilities], Optional[str]]


class HostTimeout(RuntimeError):
    """One host exceeded the per-attempt deadline. Retryable on the same host
    (up to `attempts_per_host`), then recorded as its rejection reason."""


class NoCapableHost(RuntimeError):
    """No registered host satisfies the request. Carries the full per-host
    rejection map -- the answer to "why not?" for every single candidate."""

    def __init__(self, rejections: Mapping[str, Sequence[str]]):
        self.rejections = {k: list(v) for k, v in rejections.items()}
        lines = [f"  {name}: {'; '.join(reasons)}" for name, reasons in self.rejections.items()]
        super().__init__("no capable host for this request:\n" + "\n".join(lines))


class AllHostsFailed(RuntimeError):
    """Every capable host was tried and raised at call time. Carries the same
    rejection map, now including the runtime failures, and chains the last
    exception as its cause."""

    def __init__(self, rejections: Mapping[str, Sequence[str]]):
        self.rejections = {k: list(v) for k, v in rejections.items()}
        lines = [f"  {name}: {'; '.join(reasons)}" for name, reasons in self.rejections.items()]
        super().__init__("every capable host failed at call time:\n" + "\n".join(lines))


class ValidationExhausted(RuntimeError):
    """The response failed validation and the bounded repair rounds are spent.
    Carries every error seen, in order -- never a silently coerced value."""

    def __init__(self, errors: Sequence[str]):
        self.errors = list(errors)
        super().__init__(
            f"response failed validation after {len(errors)} attempt(s): "
            + " | ".join(errors))


# -- requirement factories -------------------------------------------------------

def needs_tools() -> Requirement:
    def check(req: ChatRequest, caps: Capabilities) -> Optional[str]:
        if req.tools and not caps.supports_tools:
            return "request offers tools but host declares supports_tools=False"
        return None
    return check


def needs_json() -> Requirement:
    def check(req: ChatRequest, caps: Capabilities) -> Optional[str]:
        if req.json_only and not caps.supports_json_mode:
            return "request demands JSON mode but host declares supports_json_mode=False"
        return None
    return check


def fits_context(margin: float = 1.25) -> Requirement:
    """Reject hosts whose declared window is smaller than the request's
    ESTIMATED size times a safety margin. The estimate is chars/4 -- crude by
    design and said so; the margin absorbs the crudeness."""

    def check(req: ChatRequest, caps: Capabilities) -> Optional[str]:
        est = req.approx_tokens()
        if est * margin > caps.context_tokens:
            return (f"estimated {est} tokens x{margin} margin exceeds declared "
                    f"context of {caps.context_tokens}")
        return None
    return check


DEFAULT_REQUIREMENTS: Tuple[Requirement, ...] = (needs_tools(), needs_json(), fits_context())


@dataclass(frozen=True)
class RoutingPolicy:
    """Preference order plus qualification rules. `prefer` names adapters in
    the order to try them; registered adapters not named come after, in
    registration order. Policy is data: printable, diffable, WAL-recordable."""

    prefer: Tuple[str, ...] = ()
    requirements: Tuple[Requirement, ...] = DEFAULT_REQUIREMENTS


@dataclass
class RoutingDecision:
    """The complete, WAL-safe record of one routing pass."""

    adapter: Optional[str]                      # None until a host serves
    considered: Tuple[str, ...]
    rejections: Dict[str, List[str]] = field(default_factory=dict)
    attempts: List[str] = field(default_factory=list)   # hosts actually called
    timings: Dict[str, float] = field(default_factory=dict)  # host -> total seconds

    def reject(self, name: str, reason: str) -> None:
        self.rejections.setdefault(name, []).append(reason)

    def describe(self) -> dict:
        return {
            "adapter": self.adapter,
            "considered": list(self.considered),
            "attempts": list(self.attempts),
            "rejections": {k: list(v) for k, v in self.rejections.items()},
            "timings": {k: round(v, 3) for k, v in self.timings.items()},
        }


_REPAIR_PROMPT = (
    "Your previous reply failed validation with this error:\n{error}\n"
    "Reply again, satisfying the requirement exactly. Output only the "
    "corrected reply."
)


class Router:
    """Deterministic capability routing with per-host truthful rejection.

    Dependency-injected: adapters come in as a sequence, the breaker is the
    process-global one from `breaker.py` (or absent, in which case no health
    gating occurs and that is visible in the decision -- not guessed at).
    """

    def __init__(self, adapters: Sequence[HostAdapter],
                 policy: Optional[RoutingPolicy] = None,
                 *,
                 attempt_timeout: Optional[float] = DEFAULT_ATTEMPT_TIMEOUT,
                 attempts_per_host: int = 1,
                 backoff_base: float = 0.25):
        if not adapters:
            raise ValueError(
                "a Router with no adapters can never serve a request; register "
                "at least one HostAdapter")
        names = [a.name for a in adapters]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(
                f"adapter names must be unique; duplicated: {sorted(dupes)}. Two "
                f"adapters answering to one name makes every routing record ambiguous.")
        if attempts_per_host < 1:
            raise ValueError(f"attempts_per_host must be >= 1, got {attempts_per_host}")
        self._adapters: Dict[str, HostAdapter] = {a.name: a for a in adapters}
        self._order: Tuple[str, ...] = tuple(names)
        self.policy = policy or RoutingPolicy()
        self.attempt_timeout = attempt_timeout
        self.attempts_per_host = attempts_per_host
        """Transport-level retries against the SAME host (timeouts, connection
        resets) before falling to the next candidate. Exponential backoff from
        `backoff_base`. Distinct from validation repair, which is about the
        answer, and from the breaker, which is about giving up entirely."""
        self.backoff_base = backoff_base

    # -- introspection -------------------------------------------------------------

    def hosts(self) -> Dict[str, dict]:
        """Every registered host and its declared capabilities, in candidate
        order -- the `kit.guarantees()` idea applied to routing: an operator
        can read exactly what this router believes before trusting it."""
        return {name: self._adapters[name].capabilities().describe()
                for name in self._candidates()}

    # -- candidate ordering ------------------------------------------------------

    def _candidates(self) -> Tuple[str, ...]:
        preferred = [n for n in self.policy.prefer if n in self._adapters]
        rest = [n for n in self._order if n not in preferred]
        return tuple(preferred + rest)

    # -- pre-flight routing --------------------------------------------------------

    def route(self, request: ChatRequest) -> RoutingDecision:
        """Pick the first candidate that qualifies. Raises NoCapableHost with
        the full rejection map when none does."""
        decision = RoutingDecision(adapter=None, considered=self._candidates())
        for name in decision.considered:
            if self._qualifies(name, request, decision):
                decision.adapter = name
                return decision
        raise NoCapableHost(decision.rejections)

    def _qualifies(self, name: str, request: ChatRequest,
                   decision: RoutingDecision) -> bool:
        breaker = get_breaker()
        if breaker is not None:
            try:
                breaker.check(f"llm.{name}")
            except Exception as exc:
                decision.reject(name, f"circuit open: {exc}")
                return False
        caps = self._adapters[name].capabilities()
        ok = True
        for requirement in self.policy.requirements:
            reason = requirement(request, caps)
            if reason is not None:
                decision.reject(name, reason)
                ok = False
        return ok

    # -- routed completion -----------------------------------------------------------

    async def complete(
        self,
        request: ChatRequest,
        *,
        ctx: Any = None,
        validate: Optional[Callable[[ChatResponse], Any]] = None,
        max_repair_rounds: int = 1,
    ) -> Tuple[ChatResponse, RoutingDecision]:
        """Route, call, fall back, optionally validate-and-repair.

        Fallback walks the remaining capable candidates in policy order when a
        host raises; every runtime failure is recorded on the decision and
        reported to the breaker. When a saga context is supplied, the final
        decision -- including everything that went wrong on the way -- is
        appended to its WAL as an ``LLM_ROUTED`` event.

        Validation is caller-supplied (`schemas.validate_schema`, a pydantic
        model, any callable that raises). Repair is bounded and explicit: at
        most `max_repair_rounds` re-asks on the SAME host, each carrying the
        validator's error text. Exhaustion raises ValidationExhausted with the
        full error history.
        """
        decision = RoutingDecision(adapter=None, considered=self._candidates())
        breaker = get_breaker()
        last_exc: Optional[BaseException] = None

        try:
            for name in decision.considered:
                if not self._qualifies(name, request, decision):
                    continue
                adapter = self._adapters[name]
                decision.attempts.append(name)
                try:
                    response = await self._call_validated(
                        adapter, request, validate, max_repair_rounds, decision)
                except ValidationExhausted:
                    # The host is healthy; its *answer* is unacceptable. Falling
                    # back to a cheaper host on a validation failure would be
                    # routing on hope -- surface it instead.
                    raise
                except Exception as exc:
                    last_exc = exc
                    decision.reject(name, f"raised at call time: {exc!r}")
                    if breaker is not None:
                        breaker.record_failure(f"llm.{name}", error=repr(exc))
                    continue
                if breaker is not None:
                    breaker.record_success(f"llm.{name}")
                decision.adapter = name
                return response, decision

            if decision.attempts:
                error: RuntimeError = AllHostsFailed(decision.rejections)
                raise error from last_exc
            raise NoCapableHost(decision.rejections)
        finally:
            if ctx is not None:
                # Audit even the failures -- especially the failures. The WAL
                # event is the replayable answer to "why did this request end
                # up on that host / on no host at all?"
                ctx.wal.append("LLM_ROUTED", {
                    "saga_id": getattr(ctx, "saga_id", None),
                    **decision.describe(),
                })

    async def _call_host(self, adapter: HostAdapter, request: ChatRequest,
                         decision: RoutingDecision) -> ChatResponse:
        """One logical call: per-attempt deadline, bounded same-host retries
        with exponential backoff, wall-clock accrued onto the decision. The
        last failure propagates -- retries exhausted is a real error, not a
        quieter one."""
        from .observability.otel import get_tracer

        last: Optional[BaseException] = None
        for attempt in range(1, self.attempts_per_host + 1):
            started = time.monotonic()
            try:
                with get_tracer().span("llm.host.attempt", {
                        "llm.host": adapter.name, "llm.attempt": attempt}):
                    if self.attempt_timeout is None:
                        return await adapter.complete(request)
                    try:
                        return await asyncio.wait_for(
                            adapter.complete(request), self.attempt_timeout)
                    except asyncio.TimeoutError:
                        raise HostTimeout(
                            f"{adapter.name} gave no response within "
                            f"{self.attempt_timeout}s (attempt {attempt}/"
                            f"{self.attempts_per_host})") from None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last = exc
                if attempt < self.attempts_per_host:
                    await asyncio.sleep(self.backoff_base * (2 ** (attempt - 1)))
            finally:
                decision.timings[adapter.name] = (
                    decision.timings.get(adapter.name, 0.0)
                    + (time.monotonic() - started))
        assert last is not None
        raise last

    async def _call_validated(
        self,
        adapter: HostAdapter,
        request: ChatRequest,
        validate: Optional[Callable[[ChatResponse], Any]],
        max_repair_rounds: int,
        decision: RoutingDecision,
    ) -> ChatResponse:
        response = await self._call_host(adapter, request, decision)
        if validate is None:
            return response

        errors: List[str] = []
        current = request
        while True:
            try:
                validate(response)
                return response
            except Exception as exc:
                errors.append(repr(exc))
                if len(errors) > max_repair_rounds:
                    raise ValidationExhausted(errors) from exc
                current = ChatRequest(
                    messages=current.messages + (
                        Message(role="assistant", content=response.text),
                        Message(role="user",
                                content=_REPAIR_PROMPT.format(error=errors[-1])),
                    ),
                    tools=current.tools,
                    json_only=current.json_only,
                    max_tokens=current.max_tokens,
                    temperature=current.temperature,
                    provider_extra=current.provider_extra,
                )
                response = await self._call_host(adapter, current, decision)


__all__ = [
    "AllHostsFailed",
    "DEFAULT_ATTEMPT_TIMEOUT",
    "HostTimeout",
    "DEFAULT_REQUIREMENTS",
    "NoCapableHost",
    "Requirement",
    "Router",
    "RoutingDecision",
    "RoutingPolicy",
    "ValidationExhausted",
    "fits_context",
    "needs_json",
    "needs_tools",
]
