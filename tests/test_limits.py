"""Cumulative spend, rate, and exposure limits.

The headline case is the one a per-call threshold cannot catch: many small
calls that are each individually fine.
"""

import asyncio

import pytest

from conftest import aio
from agent_saga.gate import (
    GateContext,
    PreFlightGate,
    PreFlightViolation,
    Rule,
    Verdict,
    tool_is,
)
from agent_saga.limits import (
    BudgetLimit,
    InProcessLimitStore,
    LimitExceeded,
    LimitMisconfigured,
    RateLimit,
    Reservation,
    by_arg,
    by_tool,
    combine,
    get_limit_store,
    set_limit_store,
)
from agent_saga.semantics import ActionSemantics


@pytest.fixture(autouse=True)
def fresh_store():
    """The store is process-global; a leaked window would make tests order
    dependent in exactly the way a real budget bug hides."""
    store = InProcessLimitStore()
    set_limit_store(store)
    yield store
    set_limit_store(InProcessLimitStore())


def call(tool="stripe.charge", semantics=ActionSemantics.COMPENSABLE, **kwargs):
    return GateContext(tool=tool, semantics=semantics, kwargs=kwargs)


# ---------------------------------------------------------------------------
# 1. The gap this module exists to close
# ---------------------------------------------------------------------------

@aio
async def test_many_small_calls_cannot_exceed_the_window_budget():
    """1,000 charges of $999 pass a $1,000 per-call threshold and move
    $999,000. The same traffic against a $5,000 daily budget moves $4,995."""
    gate = PreFlightGate(rules=[], limits=[
        BudgetLimit("daily", arg="amount", max_total=5_000, window=86_400)])

    authorized = 0
    for _ in range(1_000):
        try:
            await gate.evaluate(call(amount=999))
        except PreFlightViolation:
            continue
        authorized += 999

    assert authorized == 4_995
    assert authorized <= 5_000


@aio
async def test_refusal_names_every_number_an_auditor_would_ask_for(fresh_store):
    gate = PreFlightGate(rules=[], limits=[
        BudgetLimit("daily", arg="amount", max_total=1_000, window=3_600)])
    await gate.evaluate(call(amount=900))

    with pytest.raises(PreFlightViolation) as excinfo:
        await gate.evaluate(call(amount=200))

    reason = excinfo.value.decision.reason
    assert "900" in reason and "200" in reason and "1100" in reason and "1000" in reason
    assert excinfo.value.decision.rule == "limit:daily"


# ---------------------------------------------------------------------------
# 2. Reservation lifecycle
# ---------------------------------------------------------------------------

@aio
async def test_a_rule_refusal_hands_the_budget_back(fresh_store):
    """Refusal is the one outcome where we know the effect did not happen, so
    repeated refusals must not exhaust the allowance."""
    gate = PreFlightGate(
        rules=[Rule("always-block", lambda c: True, Verdict.BLOCK, "no")],
        limits=[BudgetLimit("daily", arg="amount", max_total=100, window=60)])

    for _ in range(20):
        with pytest.raises(PreFlightViolation):
            await gate.evaluate(call(amount=100))

    assert fresh_store.usage("daily::*", 60) == 0


@aio
async def test_denied_approval_hands_the_budget_back(fresh_store):
    gate = PreFlightGate(
        rules=[Rule("needs-human", lambda c: True, Verdict.REQUIRE_APPROVAL, "ask")],
        approval_provider=lambda ctx, rule: False,
        limits=[BudgetLimit("daily", arg="amount", max_total=100, window=60)])

    with pytest.raises(PreFlightViolation):
        await gate.evaluate(call(amount=100))
    assert fresh_store.usage("daily::*", 60) == 0


@aio
async def test_authorized_spend_is_permanent_even_when_the_step_fails(fresh_store):
    """Consumption is not credited back on failure. A timed-out charge may have
    reached the card network, and an agent looping charge -> refund -> charge is
    exactly what a budget exists to stop."""
    gate = PreFlightGate(rules=[], limits=[
        BudgetLimit("daily", arg="amount", max_total=100, window=60)])

    await gate.evaluate(call(amount=60))
    # The caller now runs the tool and it explodes; the gate is not told, by
    # design. The window still shows the money as at risk.
    assert fresh_store.usage("daily::*", 60) == 60

    with pytest.raises(PreFlightViolation):
        await gate.evaluate(call(amount=60))


@aio
async def test_limits_are_all_or_nothing(fresh_store):
    """A call refused by the second limit must not leave the first debited."""
    gate = PreFlightGate(rules=[], limits=[
        BudgetLimit("generous", arg="amount", max_total=10_000, window=60),
        BudgetLimit("tight", arg="amount", max_total=50, window=60)])

    with pytest.raises(PreFlightViolation) as excinfo:
        await gate.evaluate(call(amount=100))

    assert excinfo.value.decision.rule == "limit:tight"
    assert fresh_store.usage("generous::*", 60) == 0, "partial debit leaked"


# ---------------------------------------------------------------------------
# 3. Scoping
# ---------------------------------------------------------------------------

@aio
async def test_scopes_are_independent_buckets(fresh_store):
    gate = PreFlightGate(rules=[], limits=[
        BudgetLimit("per-customer", arg="amount", max_total=100, window=60,
                    scope=by_arg("customer_id"))])

    await gate.evaluate(call(amount=100, customer_id="cus_A"))
    await gate.evaluate(call(amount=100, customer_id="cus_B"))   # own bucket

    with pytest.raises(PreFlightViolation):
        await gate.evaluate(call(amount=1, customer_id="cus_A"))


@aio
async def test_calls_missing_the_scope_argument_share_one_bucket(fresh_store):
    """A tool that forgets the dimension must be throttled with its peers, not
    handed a private unlimited bucket each time."""
    gate = PreFlightGate(rules=[], limits=[
        BudgetLimit("per-customer", arg="amount", max_total=100, window=60,
                    scope=by_arg("customer_id"))])

    await gate.evaluate(call(amount=100))
    with pytest.raises(PreFlightViolation):
        await gate.evaluate(call(amount=1))


@aio
async def test_combined_scope_intersects_dimensions(fresh_store):
    gate = PreFlightGate(rules=[], limits=[
        BudgetLimit("per-cust-per-tool", arg="amount", max_total=100, window=60,
                    scope=combine(by_tool, by_arg("customer_id")))])

    await gate.evaluate(call(tool="stripe.charge", amount=100, customer_id="A"))
    await gate.evaluate(call(tool="wire.send", amount=100, customer_id="A"))

    with pytest.raises(PreFlightViolation):
        await gate.evaluate(call(tool="stripe.charge", amount=1, customer_id="A"))


# ---------------------------------------------------------------------------
# 4. Applicability -- a money budget must not block unrelated tools
# ---------------------------------------------------------------------------

@aio
async def test_budget_ignores_calls_that_carry_no_such_argument(fresh_store):
    gate = PreFlightGate(rules=[], limits=[
        BudgetLimit("daily", arg="amount", max_total=1, window=60)])
    await gate.evaluate(call(tool="send_email", semantics=ActionSemantics.IRREVERSIBLE,
                             to="ops@example.com"))


@aio
async def test_explicitly_policed_tool_missing_its_amount_is_blocked(fresh_store):
    """The policy says police this tool; the call does not expose the amount.
    Failing open here would wave through the one call the limit was written for."""
    gate = PreFlightGate(rules=[], limits=[
        BudgetLimit("daily", arg="amount", max_total=1_000, window=60,
                    applies=tool_is("wire.send"))])

    with pytest.raises(PreFlightViolation) as excinfo:
        await gate.evaluate(call(tool="wire.send", recipient="acct_9"))
    assert excinfo.value.decision.rule == "limit-misconfigured"


@aio
async def test_a_negative_amount_cannot_mint_budget(fresh_store):
    gate = PreFlightGate(rules=[], limits=[
        BudgetLimit("daily", arg="amount", max_total=100, window=60)])
    await gate.evaluate(call(amount=100))

    with pytest.raises(PreFlightViolation):
        await gate.evaluate(call(amount=-500))
    with pytest.raises(PreFlightViolation):
        await gate.evaluate(call(amount=1))


@aio
async def test_bool_is_not_an_amount(fresh_store):
    """bool subclasses int; charge(amount=True) must not read as $1."""
    gate = PreFlightGate(rules=[], limits=[
        BudgetLimit("daily", arg="amount", max_total=100, window=60)])
    await gate.evaluate(call(amount=True))
    assert fresh_store.usage("daily::*", 60) == 0


# ---------------------------------------------------------------------------
# 5. Rate limits
# ---------------------------------------------------------------------------

@aio
async def test_rate_limit_caps_call_volume(fresh_store):
    gate = PreFlightGate(rules=[], limits=[
        RateLimit("velocity", max_calls=3, window=60, scope=by_tool)])

    for _ in range(3):
        await gate.evaluate(call(amount=1))
    with pytest.raises(PreFlightViolation) as excinfo:
        await gate.evaluate(call(amount=1))
    assert "call(s)" in excinfo.value.decision.reason


@aio
async def test_rate_limit_applies_to_tools_with_no_arguments(fresh_store):
    gate = PreFlightGate(rules=[], limits=[RateLimit("velocity", max_calls=1, window=60)])
    await gate.evaluate(call(tool="send_email"))
    with pytest.raises(PreFlightViolation):
        await gate.evaluate(call(tool="anything_else"))


# ---------------------------------------------------------------------------
# 6. Sliding window
# ---------------------------------------------------------------------------

@aio
async def test_window_slides_rather_than_resetting(fresh_store):
    gate = PreFlightGate(rules=[], limits=[
        BudgetLimit("burst", arg="amount", max_total=100, window=0.15)])

    await gate.evaluate(call(amount=100))
    with pytest.raises(PreFlightViolation):
        await gate.evaluate(call(amount=100))

    await asyncio.sleep(0.2)
    await gate.evaluate(call(amount=100))       # oldest entry has aged out


# ---------------------------------------------------------------------------
# 7. Escalation to a human
# ---------------------------------------------------------------------------

@aio
async def test_overage_can_route_to_a_human_instead_of_a_hard_block(fresh_store):
    asked = []

    def approver(ctx, rule):
        asked.append(rule.reason)
        return True

    gate = PreFlightGate(rules=[], approval_provider=approver, limits=[
        BudgetLimit("daily", arg="amount", max_total=100, window=60,
                    escalate_to_human=True)])

    await gate.evaluate(call(amount=100))
    await gate.evaluate(call(amount=500))       # over budget, approved

    assert len(asked) == 1 and "would reach 600" in asked[0]


@aio
async def test_an_approved_overage_is_still_recorded(fresh_store):
    """Skipping the record would make the next call look like it had fresh
    budget, so one approval would silently authorize the rest of the day."""
    gate = PreFlightGate(rules=[], approval_provider=lambda ctx, rule: True, limits=[
        BudgetLimit("daily", arg="amount", max_total=100, window=60,
                    escalate_to_human=True)])

    await gate.evaluate(call(amount=500))
    assert fresh_store.usage("daily::*", 60) == 500


@aio
async def test_denied_overage_blocks(fresh_store):
    gate = PreFlightGate(rules=[], approval_provider=lambda ctx, rule: False, limits=[
        BudgetLimit("daily", arg="amount", max_total=100, window=60,
                    escalate_to_human=True)])
    with pytest.raises(PreFlightViolation) as excinfo:
        await gate.evaluate(call(amount=500))
    assert "denied" in excinfo.value.decision.reason


@aio
async def test_escalation_without_an_approval_provider_fails_closed(fresh_store):
    gate = PreFlightGate(rules=[], limits=[
        BudgetLimit("daily", arg="amount", max_total=100, window=60,
                    escalate_to_human=True)])
    with pytest.raises(PreFlightViolation) as excinfo:
        await gate.evaluate(call(amount=500))
    assert "No approval provider" in excinfo.value.decision.reason


@aio
async def test_each_exceeded_limit_is_escalated_once(fresh_store):
    """Two limits both over budget must resolve, not spin."""
    gate = PreFlightGate(rules=[], approval_provider=lambda ctx, rule: True, limits=[
        BudgetLimit("a", arg="amount", max_total=10, window=60, escalate_to_human=True),
        BudgetLimit("b", arg="amount", max_total=20, window=60, escalate_to_human=True)])
    await gate.evaluate(call(amount=500))
    assert fresh_store.usage("a::*", 60) == 500
    assert fresh_store.usage("b::*", 60) == 500


# ---------------------------------------------------------------------------
# 8. Fail-closed on infrastructure trouble
# ---------------------------------------------------------------------------

class BrokenStore:
    distributed = True

    def reserve(self, requests):
        raise ConnectionError("redis is down")

    def release(self, reservation):
        raise ConnectionError("redis is down")

    def usage(self, key, window):
        return 0.0


@aio
async def test_an_unreachable_store_blocks_rather_than_allowing_unmetered():
    """A limiter that passes calls through when its backend is down is not a
    limiter."""
    set_limit_store(BrokenStore())
    gate = PreFlightGate(rules=[], limits=[
        BudgetLimit("daily", arg="amount", max_total=100, window=60)])

    with pytest.raises(PreFlightViolation) as excinfo:
        await gate.evaluate(call(amount=1))
    assert excinfo.value.decision.rule == "limit-store-unavailable"


@aio
async def test_a_failed_release_does_not_mask_the_refusal():
    """Losing a release over-counts, which errs toward refusing later calls --
    the safe direction. It must not replace the exception the caller needs."""
    set_limit_store(_ReserveOkReleaseBroken())
    gate = PreFlightGate(
        rules=[Rule("always-block", lambda c: True, Verdict.BLOCK, "no")],
        limits=[BudgetLimit("daily", arg="amount", max_total=100, window=60)])

    with pytest.raises(PreFlightViolation) as excinfo:
        await gate.evaluate(call(amount=1))
    assert excinfo.value.decision.rule == "always-block"


class _ReserveOkReleaseBroken(InProcessLimitStore):
    def release(self, reservation):
        raise ConnectionError("redis died between reserve and release")


# ---------------------------------------------------------------------------
# 9. Async (distributed) stores
# ---------------------------------------------------------------------------

class AsyncStore:
    """Mirrors RedisLimitStore's async surface without needing Redis."""

    distributed = True

    def __init__(self):
        self._inner = InProcessLimitStore()
        self.released = 0

    async def reserve(self, requests):
        await asyncio.sleep(0)
        return self._inner.reserve(requests)

    async def release(self, reservation):
        await asyncio.sleep(0)
        self.released += 1
        return self._inner.release(reservation)

    async def usage(self, key, window):
        return self._inner.usage(key, window)


@aio
async def test_gate_awaits_a_distributed_store():
    store = AsyncStore()
    set_limit_store(store)
    gate = PreFlightGate(rules=[], limits=[
        BudgetLimit("daily", arg="amount", max_total=100, window=60)])

    await gate.evaluate(call(amount=100))
    with pytest.raises(PreFlightViolation):
        await gate.evaluate(call(amount=1))


@aio
async def test_gate_awaits_release_on_a_distributed_store():
    store = AsyncStore()
    set_limit_store(store)
    gate = PreFlightGate(
        rules=[Rule("always-block", lambda c: True, Verdict.BLOCK, "no")],
        limits=[BudgetLimit("daily", arg="amount", max_total=100, window=60)])

    with pytest.raises(PreFlightViolation):
        await gate.evaluate(call(amount=100))
    assert store.released == 1
    assert await store.usage("daily::*", 60) == 0


# ---------------------------------------------------------------------------
# 10. Concurrency
# ---------------------------------------------------------------------------

@aio
async def test_concurrent_calls_cannot_race_past_the_cap(fresh_store):
    """Check-then-debit as two steps would let every coroutine read the same
    low usage and all decide they fit."""
    gate = PreFlightGate(rules=[], limits=[
        BudgetLimit("daily", arg="amount", max_total=1_000, window=60)])

    async def attempt():
        try:
            await gate.evaluate(call(amount=100))
            return 100
        except PreFlightViolation:
            return 0

    granted = sum(await asyncio.gather(*(attempt() for _ in range(50))))
    assert granted == 1_000
    assert fresh_store.usage("daily::*", 60) == 1_000


def test_threads_cannot_race_past_the_cap():
    """The in-process store is reachable from the tool executor's threads."""
    import threading

    store = InProcessLimitStore()
    granted = []
    lock = threading.Lock()

    def worker():
        from agent_saga.limits import LimitRequest

        out = store.reserve([LimitRequest("daily", "k", 1.0, 100.0, 60.0)])
        if isinstance(out, Reservation):
            with lock:
                granted.append(1)

    threads = [threading.Thread(target=worker) for _ in range(300)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(granted) == 100


# ---------------------------------------------------------------------------
# 11. Misconfiguration is caught at authoring time
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {"arg": "", "max_total": 10, "window": 60},
    {"arg": "amount", "max_total": 0, "window": 60},
    {"arg": "amount", "max_total": -5, "window": 60},
    {"arg": "amount", "max_total": 10, "window": 0},
])
def test_bad_budget_raises_when_declared(kwargs):
    with pytest.raises(LimitMisconfigured):
        BudgetLimit("bad", **kwargs)


@pytest.mark.parametrize("kwargs", [
    {"max_calls": 0, "window": 60},
    {"max_calls": 5, "window": -1},
])
def test_bad_rate_limit_raises_when_declared(kwargs):
    with pytest.raises(LimitMisconfigured):
        RateLimit("bad", **kwargs)


def test_the_default_store_declares_that_it_is_not_distributed():
    """A local budget fails *open* across a fleet -- n pods, n allowances. The
    flag is how a deployment check can catch that."""
    assert get_limit_store().distributed is False
