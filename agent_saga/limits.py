"""Cumulative spend, rate, and exposure limits for the pre-flight gate.

`arg_exceeds("amount", 1000)` inspects one call in isolation, which means an
agent that issues 1,000 charges of $999 satisfies every check and moves
$999,000. Per-call thresholds cannot express "no more than $50k a day" -- that
is a statement about a *window*, and answering it requires state.

Three properties carry the safety here:

  * **All-or-nothing.** A call policed by several limits consumes all of their
    budgets or none, so a call refused by the third limit does not silently
    leave the first two debited.

  * **Consumption survives failure.** Once the gate authorizes a call, its
    budget is spent -- even if the step then raises, and even if the saga later
    compensates. A timed-out charge may well have reached the card network
    (`STEP_UNKNOWN` in context.py takes the same position), so crediting it back
    would let a failing agent spend without bound. The meter measures *gross
    authorized outflow*, not net balance, and that is deliberate: an agent
    looping charge -> refund -> charge is exactly the behaviour a limit exists to
    stop, and a net meter would never see it.

  * **Refusal releases.** A reservation taken before a later rule blocks the
    call is returned, because refusal is the one moment we can be *certain* the
    effect did not happen -- the same argument the gate itself is built on.

SCOPE: the default store is process-local. Unlike a local lock, which merely
fails to coordinate, a local *budget* fails **open** across a fleet: ten pods
each grant the full daily allowance, so the effective limit is ten times the one
you configured, and nothing warns you. Use `RedisLimitStore` for anything
running more than one process.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, Sequence, runtime_checkable

logger = logging.getLogger("agent_saga")

ScopeFn = Callable[[Any], str]
"""GateContext -> a string identifying which bucket this call draws from."""


# ---------------------------------------------------------------------------
# Scope: which bucket does this call draw from?
# ---------------------------------------------------------------------------

def GLOBAL(ctx: Any) -> str:
    """One shared bucket for every call the limit applies to."""
    return "*"


def by_tool(ctx: Any) -> str:
    """A separate bucket per tool. `stripe.charge` and `wire.send` each get
    their own allowance rather than competing for one."""
    return f"tool={ctx.tool}"


def by_arg(name: str, *, missing: str = "\x00missing") -> ScopeFn:
    """A separate bucket per value of an argument -- `by_arg("customer_id")`
    enforces "no more than $1k/day to any one customer".

    A call that omits the argument lands in a single shared `missing` bucket
    rather than each getting a private unlimited one, so a tool that forgets to
    declare the dimension is throttled together instead of escaping the limit.
    """

    def _scope(ctx: Any) -> str:
        value = ctx.kwargs.get(name, missing)
        return f"{name}={value}"

    return _scope


def combine(*scopes: ScopeFn) -> ScopeFn:
    """Intersect dimensions: `combine(by_tool, by_arg("customer_id"))` is
    "per customer, per tool"."""

    def _scope(ctx: Any) -> str:
        return "|".join(s(ctx) for s in scopes)

    return _scope


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

class LimitMisconfigured(Exception):
    """A limit was asked to police a call it cannot measure.

    Raised at authoring time where possible, and converted to a BLOCK at
    evaluation time otherwise -- a limit that cannot read the amount it is
    supposed to cap must not quietly pass the call through.
    """


@dataclass(frozen=True)
class Limit:
    """Base for the windowed limits. Not used directly."""

    name: str
    window: float
    """Seconds. The window slides: usage is the sum over `now - window .. now`,
    not a bucket that resets on the hour. A fixed window would let an agent
    spend a full allowance at 23:59 and another at 00:01."""
    scope: ScopeFn = GLOBAL
    applies: Optional[Callable[[Any], bool]] = None
    escalate_to_human: bool = False
    """False (default) refuses outright. True routes to the gate's approval
    provider instead, which is what "over $50k/day needs a director" looks
    like. An approved overage is still recorded against the window."""

    def _applies(self, ctx: Any) -> bool:
        raise NotImplementedError

    def _amount(self, ctx: Any) -> float:
        raise NotImplementedError

    def _cap(self) -> float:
        raise NotImplementedError

    def _unit(self) -> str:
        return ""


@dataclass(frozen=True)
class BudgetLimit(Limit):
    """Cap the *sum* of a numeric argument over a sliding window.

        BudgetLimit("daily-charges", arg="amount", max_total=50_000,
                    window=86_400, scope=by_tool)

    This is the control `arg_exceeds` cannot express.
    """

    arg: str = ""
    max_total: float = 0.0

    def __post_init__(self) -> None:
        if not self.arg:
            raise LimitMisconfigured(
                f"BudgetLimit {self.name!r} needs `arg` -- the name of the "
                f"numeric argument to sum (e.g. arg='amount')."
            )
        if self.max_total <= 0:
            raise LimitMisconfigured(
                f"BudgetLimit {self.name!r} has max_total={self.max_total}. A "
                f"non-positive cap would refuse every call; if you meant to "
                f"disable the limit, remove it."
            )
        if self.window <= 0:
            raise LimitMisconfigured(
                f"BudgetLimit {self.name!r} has window={self.window}.")

    def _applies(self, ctx: Any) -> bool:
        if self.applies is not None:
            return bool(self.applies(ctx))
        # Default: police any call that actually carries the argument. Without
        # this a global budget on `amount` would block `send_email`, which has
        # no amount to measure and no business being charged against it.
        return _numeric(ctx.kwargs.get(self.arg)) is not None

    def _amount(self, ctx: Any) -> float:
        value = _numeric(ctx.kwargs.get(self.arg))
        if value is None:
            # Only reachable when an explicit `applies` selected this call. The
            # policy says "police this tool" and the call does not expose the
            # amount, so the limit cannot do its job -- fail closed rather than
            # wave through the one call it was written to catch.
            raise LimitMisconfigured(
                f"limit {self.name!r} applies to {ctx.tool!r} but its argument "
                f"{self.arg!r} is missing or not numeric "
                f"(got {ctx.kwargs.get(self.arg)!r}). Declare it in "
                f"`policy_args` so the gate can see it."
            )
        if value < 0:
            # A negative amount would *refund* budget and let an agent mint
            # allowance by alternating signs.
            raise LimitMisconfigured(
                f"limit {self.name!r} got a negative {self.arg}={value!r} from "
                f"{ctx.tool!r}. Model refunds as their own tool, not as a "
                f"negative charge."
            )
        return float(value)

    def _cap(self) -> float:
        return float(self.max_total)


@dataclass(frozen=True)
class RateLimit(Limit):
    """Cap the *number of calls* over a sliding window.

        RateLimit("charge-velocity", max_calls=20, window=60, scope=by_tool)

    Volume is its own risk even when every individual call is small.
    """

    max_calls: int = 0

    def __post_init__(self) -> None:
        if self.max_calls <= 0:
            raise LimitMisconfigured(
                f"RateLimit {self.name!r} has max_calls={self.max_calls}.")
        if self.window <= 0:
            raise LimitMisconfigured(
                f"RateLimit {self.name!r} has window={self.window}.")

    def _applies(self, ctx: Any) -> bool:
        return True if self.applies is None else bool(self.applies(ctx))

    def _amount(self, ctx: Any) -> float:
        return 1.0

    def _cap(self) -> float:
        return float(self.max_calls)

    def _unit(self) -> str:
        return " call(s)"


def _numeric(value: Any) -> Optional[float]:
    """bool is an int in Python; `charge(amount=True)` must not read as 1."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# ---------------------------------------------------------------------------
# Store protocol and results
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LimitRequest:
    """One limit's claim on one bucket, for one call."""

    limit_name: str
    key: str
    amount: float
    cap: float
    window: float


@dataclass(frozen=True)
class LimitExceeded:
    """Why a reservation was refused. Every number an auditor would ask for."""

    limit_name: str
    key: str
    used: float
    requested: float
    cap: float
    window: float

    @property
    def would_reach(self) -> float:
        return self.used + self.requested

    def describe(self, unit: str = "") -> str:
        return (
            f"limit {self.limit_name!r} for scope {self.key!r}: "
            f"{self.used:g}{unit} already used in the last {self.window:g}s, "
            f"this call adds {self.requested:g}{unit}, which would reach "
            f"{self.would_reach:g}{unit} against a cap of {self.cap:g}{unit}"
        )


@dataclass(frozen=True)
class Reservation:
    """A handle to budget already debited. Returned so a later refusal can
    hand it back; ignored on the success path, where it is permanent."""

    entries: tuple[tuple[str, str], ...] = ()   # (key, member)


@runtime_checkable
class LimitStore(Protocol):
    distributed: bool

    def reserve(self, requests: Sequence[LimitRequest]) -> Any:
        """Atomically debit every request, or none.

        Returns a `Reservation` on success, or a `LimitExceeded` naming the
        first limit that would be breached. Must never partially apply.

        May return an awaitable: a distributed store is a network round trip,
        and the gate awaits whatever comes back. Doing that call synchronously
        would stall every other in-flight saga on the process for the duration
        of a Redis hop -- on the hot path of every gated tool call.
        """
        ...

    def release(self, reservation: Reservation) -> None:
        """Hand back a reservation. Only ever called when the gate refused, so
        the effect provably did not happen. May return an awaitable."""
        ...

    def usage(self, key: str, window: float) -> float:
        """Current usage in the window. Observability only; never authoritative
        for a decision, because it is not atomic with the debit."""
        ...


# ---------------------------------------------------------------------------
# In-process store
# ---------------------------------------------------------------------------

class InProcessLimitStore:
    """Sliding-log limiter for a single process.

    A log of (timestamp, amount) rather than a counter, for two reasons: it is
    exact at the window edge, and it is *itemizable* -- when a risk officer asks
    what made up the $47k, the entries are still there. Money windows carry
    hundreds of events, not millions, so the memory is affordable.

    NOT distributed. Every process gets its own allowance; see the module
    docstring for why that fails open rather than merely failing to coordinate.
    """

    distributed = False

    def __init__(self) -> None:
        self._log: dict[str, list[tuple[float, float, str]]] = {}
        self._mutex = threading.Lock()
        self._counter = 0

    def _prune(self, key: str, now: float, window: float) -> float:
        entries = self._log.get(key)
        if not entries:
            return 0.0
        cutoff = now - window
        live = [e for e in entries if e[0] > cutoff]
        if live:
            self._log[key] = live
        else:
            self._log.pop(key, None)
        return sum(e[1] for e in live)

    def reserve(self, requests: Sequence[LimitRequest]) -> Any:
        now = time.time()
        with self._mutex:
            # Pass 1: check every request before mutating anything, so a
            # refusal on the last limit leaves the earlier ones untouched.
            for req in requests:
                used = self._prune(req.key, now, req.window)
                if used + req.amount > req.cap:
                    return LimitExceeded(req.limit_name, req.key, used,
                                         req.amount, req.cap, req.window)
            # Pass 2: commit.
            entries: list[tuple[str, str]] = []
            for req in requests:
                self._counter += 1
                member = f"{now:.6f}-{os.getpid()}-{self._counter}"
                self._log.setdefault(req.key, []).append((now, req.amount, member))
                entries.append((req.key, member))
            return Reservation(tuple(entries))

    def release(self, reservation: Reservation) -> None:
        with self._mutex:
            for key, member in reservation.entries:
                entries = self._log.get(key)
                if not entries:
                    continue
                self._log[key] = [e for e in entries if e[2] != member]
                if not self._log[key]:
                    self._log.pop(key, None)

    def usage(self, key: str, window: float) -> float:
        with self._mutex:
            return self._prune(key, time.time(), window)

    def reset(self) -> None:
        """Drop all state. For tests -- never call this in production, it
        forgives every limit at once."""
        with self._mutex:
            self._log.clear()


# ---------------------------------------------------------------------------
# Redis store
# ---------------------------------------------------------------------------

class RedisLimitStore:
    """Sliding-log limiter shared across every node.

    A budget is the one control that must not be process-local: `n` replicas of
    a local limiter grant `n` times the configured allowance, silently. This
    keeps one window in Redis so the cap means what it says however many pods
    are running.

    The check-and-debit is a single Lua script, which Redis runs atomically.
    Doing it as GET-then-SET from Python would let two nodes both read $49k,
    both decide $1k fits, and both spend -- the classic lost-update race, on
    money.

    Requires `pip install agent-saga[redis]`.
    """

    distributed = True

    # KEYS = bucket keys. ARGV[1] = now (seconds, float as string),
    # ARGV[2] = JSON array of {amount, cap, window, member}, aligned with KEYS.
    #
    # Two passes on purpose: every bucket is checked before any is written, so
    # a call refused by the last limit does not leave the first ones debited.
    _RESERVE_LUA = """
    local now = tonumber(ARGV[1])
    local reqs = cjson.decode(ARGV[2])
    for i, r in ipairs(reqs) do
        local cutoff = now - r.window
        redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', cutoff)
        local used = 0
        local members = redis.call('ZRANGE', KEYS[i], 0, -1)
        for _, m in ipairs(members) do
            local amt = string.match(m, '^([^|]*)')
            used = used + tonumber(amt)
        end
        if used + r.amount > r.cap then
            return cjson.encode({exceeded = i, used = used})
        end
        reqs[i].used = used
    end
    for i, r in ipairs(reqs) do
        redis.call('ZADD', KEYS[i], now, r.amount .. '|' .. r.member)
        -- Expire the whole bucket a window after its last write, so idle
        -- scopes (a customer id seen once) do not accumulate forever.
        redis.call('PEXPIRE', KEYS[i], math.ceil(r.window * 1000) + 1000)
    end
    return cjson.encode({exceeded = 0})
    """

    _RELEASE_LUA = """
    for i = 1, #KEYS do
        redis.call('ZREM', KEYS[i], ARGV[i])
    end
    return 1
    """

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        *,
        client: Any = None,
        key_prefix: str = "agent-saga:limit:",
    ):
        self.url = url
        self.key_prefix = key_prefix
        self._client = client
        self._owns_client = client is None
        self._counter = 0
        self._mutex = threading.Lock()

        if client is None:
            try:
                import redis.asyncio  # noqa: F401
            except ImportError as exc:
                raise ImportError(
                    "RedisLimitStore needs the 'redis' package.\n"
                    "    pip install agent-saga[redis]"
                ) from exc

    def _key(self, key: str) -> str:
        return f"{self.key_prefix}{key}"

    async def _conn(self) -> Any:
        if self._client is None:
            from redis.asyncio import Redis

            self._client = Redis.from_url(self.url, decode_responses=True)
        return self._client

    async def health_check(self) -> None:
        """Round-trip PING so a misconfigured or unreachable Redis surfaces at
        SagaConfig.validate() time, not on the first budget reservation."""
        conn = await self._conn()
        await conn.ping()

    def _member(self) -> str:
        with self._mutex:
            self._counter += 1
            return f"{os.getpid()}-{os.urandom(4).hex()}-{self._counter}"

    async def reserve(self, requests: Sequence[LimitRequest]) -> Any:
        if not requests:
            return Reservation()
        now = time.time()
        members = [self._member() for _ in requests]
        keys = [self._key(r.key) for r in requests]
        payload = json.dumps([
            {"amount": r.amount, "cap": r.cap, "window": r.window, "member": m}
            for r, m in zip(requests, members)
        ])
        conn = await self._conn()
        raw = await conn.eval(self._RESERVE_LUA, len(keys), *keys, str(now), payload)
        outcome = json.loads(raw)
        index = int(outcome.get("exceeded", 0))
        if index:
            req = requests[index - 1]
            return LimitExceeded(req.limit_name, req.key,
                                 float(outcome.get("used", 0.0)),
                                 req.amount, req.cap, req.window)
        return Reservation(tuple(
            (r.key, f"{r.amount}|{m}") for r, m in zip(requests, members)))

    async def release(self, reservation: Reservation) -> None:
        if not reservation.entries:
            return
        keys = [self._key(k) for k, _ in reservation.entries]
        members = [m for _, m in reservation.entries]
        conn = await self._conn()
        await conn.eval(self._RELEASE_LUA, len(keys), *keys, *members)

    async def usage(self, key: str, window: float) -> float:
        conn = await self._conn()
        full = self._key(key)
        now = time.time()
        await conn.zremrangebyscore(full, "-inf", now - window)
        members = await conn.zrange(full, 0, -1)
        return sum(float(str(m).split("|", 1)[0]) for m in members)

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            close = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
            if close is not None:
                await close()
        self._client = None


# ---------------------------------------------------------------------------
# Planning: turn limits + a call into store requests
# ---------------------------------------------------------------------------

def plan(limits: Sequence[Limit], ctx: Any) -> list[tuple[Limit, LimitRequest]]:
    """Select the limits that police this call and price each one.

    A `LimitMisconfigured` raised here propagates: the gate turns it into a
    BLOCK, because a limit that cannot measure the call it was told to police
    must not let it through.
    """
    out: list[tuple[Limit, LimitRequest]] = []
    for limit in limits:
        if not limit._applies(ctx):
            continue
        out.append((limit, LimitRequest(
            limit_name=limit.name,
            key=f"{limit.name}::{limit.scope(ctx)}",
            amount=limit._amount(ctx),
            cap=limit._cap(),
            window=limit.window,
        )))
    return out


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------

_LIMIT_STORE: Any = InProcessLimitStore()


def get_limit_store() -> Any:
    return _LIMIT_STORE


def set_limit_store(store: Any) -> None:
    """Inject a shared store. Required for any multi-process deployment."""
    global _LIMIT_STORE
    _LIMIT_STORE = store


__all__ = [
    "Limit", "BudgetLimit", "RateLimit", "LimitMisconfigured",
    "LimitRequest", "LimitExceeded", "Reservation",
    "LimitStore", "InProcessLimitStore", "RedisLimitStore",
    "GLOBAL", "by_tool", "by_arg", "combine", "plan",
    "get_limit_store", "set_limit_store",
]
