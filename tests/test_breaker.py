"""Circuit breaker.

The interesting tests are not "does it trip" -- they are the three places where
the obvious behaviour is wrong: counting refusals as failures, blocking
compensations, and failing closed when its own store is down.
"""

import time

import pytest

from conftest import aio

from agent_saga.breaker import (
    CLOSED,
    HALF_OPEN,
    OPEN,
    BreakerPolicy,
    CircuitBreaker,
    CircuitOpen,
    InProcessBreakerStore,
    get_breaker,
    set_breaker,
)
from agent_saga.context import SagaContext
from agent_saga.gate import GateContext, PreFlightGate, PreFlightViolation, Rule, Verdict
from agent_saga.limits import BudgetLimit, InProcessLimitStore, set_limit_store
from agent_saga.semantics import ActionSemantics, Compensation
from agent_saga.wal import FileWAL


@pytest.fixture
def breaker():
    b = CircuitBreaker(BreakerPolicy(failure_threshold=3, cool_down=0.2,
                                     min_volume=100))
    set_breaker(b)
    yield b
    set_breaker(None)


def a_call(tool="stripe.charge"):
    return GateContext(tool=tool, semantics=ActionSemantics.COMPENSABLE,
                       kwargs={"amount": 100})


# ---------------------------------------------------------------------------
# 1. Tripping and recovering
# ---------------------------------------------------------------------------

def test_consecutive_failures_open_the_circuit(breaker):
    for _ in range(2):
        breaker.record_failure("stripe.charge", "timeout")
    breaker.check("stripe.charge")              # still closed

    breaker.record_failure("stripe.charge", "timeout")
    with pytest.raises(CircuitOpen) as excinfo:
        breaker.check("stripe.charge")
    assert excinfo.value.state == OPEN and excinfo.value.failures == 3


def test_a_success_resets_the_consecutive_count(breaker):
    breaker.record_failure("stripe.charge")
    breaker.record_failure("stripe.charge")
    breaker.record_success("stripe.charge")
    breaker.record_failure("stripe.charge")
    breaker.check("stripe.charge")              # 1 consecutive, not 3


def test_a_failure_rate_needs_volume_behind_it():
    """Two failures out of two is 100% and is evidence of nothing."""
    b = CircuitBreaker(BreakerPolicy(failure_threshold=99, min_volume=10,
                                     failure_rate=0.5, window=60))
    for _ in range(2):
        b.record_failure("t")
    b.check("t")                                # 100% failure, no volume

    for _ in range(4):
        b.record_success("t")
    for _ in range(4):
        b.record_failure("t")
    with pytest.raises(CircuitOpen):
        b.check("t")                            # 6/10 over threshold


def test_the_circuit_half_opens_after_the_cool_down(breaker):
    for _ in range(3):
        breaker.record_failure("stripe.charge")
    with pytest.raises(CircuitOpen):
        breaker.check("stripe.charge")

    time.sleep(0.25)
    breaker.check("stripe.charge")              # one probe allowed through
    assert breaker.status()["stripe.charge"]["state"] == HALF_OPEN


def test_only_one_probe_is_allowed_in_flight(breaker):
    """More than one turns recovery into a thundering herd against something
    still fragile."""
    for _ in range(3):
        breaker.record_failure("stripe.charge")
    time.sleep(0.25)

    breaker.check("stripe.charge")
    with pytest.raises(CircuitOpen) as excinfo:
        breaker.check("stripe.charge")
    assert "already in flight" in str(excinfo.value)


def test_a_successful_probe_closes_the_circuit(breaker):
    for _ in range(3):
        breaker.record_failure("stripe.charge")
    time.sleep(0.25)
    breaker.check("stripe.charge")
    breaker.record_success("stripe.charge")

    assert breaker.status()["stripe.charge"]["state"] == CLOSED
    breaker.check("stripe.charge")


def test_a_failed_probe_reopens_with_a_fresh_cool_down(breaker):
    """Letting the next caller probe immediately would hammer something that
    just said it is still broken."""
    for _ in range(3):
        breaker.record_failure("stripe.charge")
    time.sleep(0.25)
    breaker.check("stripe.charge")
    breaker.record_failure("stripe.charge")

    with pytest.raises(CircuitOpen):
        breaker.check("stripe.charge")


def test_circuits_are_per_tool(breaker):
    for _ in range(3):
        breaker.record_failure("stripe.charge")
    with pytest.raises(CircuitOpen):
        breaker.check("stripe.charge")
    breaker.check("salesforce.patch")


def test_an_operator_can_force_it_closed(breaker):
    for _ in range(3):
        breaker.record_failure("stripe.charge")
    with pytest.raises(CircuitOpen):
        breaker.check("stripe.charge")

    breaker.reset("stripe.charge")
    breaker.check("stripe.charge")


# ---------------------------------------------------------------------------
# 2. A refusal is not a failure
# ---------------------------------------------------------------------------

@aio
async def test_policy_refusals_do_not_trip_the_breaker(breaker):
    """Counting refusals would trip the breaker exactly when the controls were
    doing their job, and it would then block the calls that were still fine."""
    gate = PreFlightGate(rules=[
        Rule("no-charges", lambda c: c.tool == "stripe.charge",
             Verdict.BLOCK, "not allowed")])

    for _ in range(10):
        with pytest.raises(PreFlightViolation):
            await gate.evaluate(a_call())

    assert breaker.status().get("stripe.charge", {}).get("state", CLOSED) == CLOSED


@aio
async def test_budget_refusals_do_not_trip_the_breaker(breaker):
    set_limit_store(InProcessLimitStore())
    try:
        gate = PreFlightGate(rules=[], limits=[
            BudgetLimit("daily", arg="amount", max_total=100, window=60)])
        await gate.evaluate(a_call())
        for _ in range(10):
            with pytest.raises(PreFlightViolation):
                await gate.evaluate(a_call())
        assert breaker.status().get("stripe.charge", {}).get("state", CLOSED) == CLOSED
    finally:
        set_limit_store(InProcessLimitStore())


# ---------------------------------------------------------------------------
# 3. Wiring through a real saga
# ---------------------------------------------------------------------------

async def run_step(ctx, tool="stripe.charge", fail=False):
    async def forward():
        if fail:
            raise ConnectionError("stripe timed out")
        return {"id": "ch_1"}

    return await ctx.execute(
        tool=tool, semantics=ActionSemantics.COMPENSABLE,
        forward=forward, forward_kwargs={},
        compensate=lambda r: Compensation(fn=lambda **kw: None,
                                          handler="x.refund", kwargs={}))


@aio
async def test_real_step_failures_open_the_circuit(breaker, tmp_path):
    wal = FileWAL(tmp_path / "w.jsonl")
    await wal.start()
    ctx = SagaContext(gate=PreFlightGate(rules=[]), wal=wal)
    await ctx.begin()

    for _ in range(3):
        with pytest.raises(ConnectionError):
            await run_step(ctx, fail=True)

    with pytest.raises(PreFlightViolation) as excinfo:
        await run_step(ctx)
    assert "circuit-open" in excinfo.value.decision.rule
    await wal.close()


@aio
async def test_successful_steps_keep_it_closed(breaker, tmp_path):
    wal = FileWAL(tmp_path / "w.jsonl")
    await wal.start()
    ctx = SagaContext(gate=PreFlightGate(rules=[]), wal=wal)
    await ctx.begin()
    for _ in range(5):
        await run_step(ctx)
    await run_step(ctx)
    await wal.close()


@aio
async def test_an_open_circuit_does_not_consume_budget(breaker):
    """No point spending a daily allowance on a call to something known down."""
    store = InProcessLimitStore()
    set_limit_store(store)
    try:
        gate = PreFlightGate(rules=[], limits=[
            BudgetLimit("daily", arg="amount", max_total=10_000, window=60)])
        for _ in range(3):
            breaker.record_failure("stripe.charge")

        for _ in range(5):
            with pytest.raises(PreFlightViolation):
                await gate.evaluate(a_call())
        assert store.usage("daily::*", 60) == 0
    finally:
        set_limit_store(InProcessLimitStore())


# ---------------------------------------------------------------------------
# 4. A breaker must never block a rollback
# ---------------------------------------------------------------------------

@aio
async def test_an_open_circuit_does_not_block_compensations(breaker, tmp_path):
    """Blocking compensations because their connector looks sick would strand
    money mid-transaction -- turning a dependency outage into a financial one."""
    wal = FileWAL(tmp_path / "w.jsonl")
    await wal.start()
    ctx = SagaContext(gate=PreFlightGate(rules=[]), wal=wal)
    await ctx.begin()

    undone = []
    await ctx.execute(
        tool="stripe.charge", semantics=ActionSemantics.COMPENSABLE,
        forward=lambda: {"id": "ch_1"}, forward_kwargs={},
        compensate=lambda r: Compensation(
            fn=lambda **kw: undone.append("refunded"),
            handler="stripe.refund", kwargs={}))

    # The connector now goes down hard.
    for _ in range(5):
        breaker.record_failure("stripe.charge")
        breaker.record_failure("stripe.refund")

    report = await ctx.rollback()
    await wal.close()

    assert undone == ["refunded"], "an open circuit blocked a rollback"
    assert report.clean


@aio
async def test_compensation_failures_do_not_feed_the_breaker(breaker, tmp_path):
    """A breaker that learned from rollback failures would open on the very
    connector whose compensations are failing, then refuse the rest of them."""
    wal = FileWAL(tmp_path / "w.jsonl")
    await wal.start()
    ctx = SagaContext(gate=PreFlightGate(rules=[]), wal=wal,
                      halt_on_compensation_failure=False)
    await ctx.begin()

    def boom(**kwargs):
        raise ConnectionError("refund failed")

    for index in range(3):
        await ctx.execute(
            tool="stripe.charge", semantics=ActionSemantics.COMPENSABLE,
            forward=lambda: {"id": f"ch_{index}"}, forward_kwargs={},
            compensate=lambda r: Compensation(fn=boom, handler="stripe.refund",
                                              kwargs={}))

    await ctx.rollback()
    await wal.close()

    assert breaker.status().get("stripe.refund", {}).get("state", CLOSED) == CLOSED


# ---------------------------------------------------------------------------
# 5. It fails OPEN, and that is not an inconsistency
# ---------------------------------------------------------------------------

class BrokenStore:
    distributed = True

    def get(self, key):
        raise ConnectionError("redis is down")

    def put(self, state):
        raise ConnectionError("redis is down")

    def all(self):
        raise ConnectionError("redis is down")


def test_an_unreachable_store_still_lets_calls_through():
    """A budget that cannot be verified must refuse, because failing open means
    overspending. A breaker is an availability protection: refusing all work
    because its own store is down would be an outage it invented."""
    b = CircuitBreaker(BreakerPolicy(failure_threshold=3), store=BrokenStore())
    b.check("stripe.charge")            # no exception


def test_a_degraded_breaker_still_protects_per_process():
    """Degrading to per-process protection beats degrading to none."""
    b = CircuitBreaker(BreakerPolicy(failure_threshold=3, cool_down=60),
                       store=BrokenStore())
    for _ in range(3):
        b.record_failure("stripe.charge")
    with pytest.raises(CircuitOpen):
        b.check("stripe.charge")


def test_the_default_store_declares_that_it_is_not_distributed():
    assert InProcessBreakerStore().distributed is False


# ---------------------------------------------------------------------------
# 6. Audit and installation
# ---------------------------------------------------------------------------

@aio
async def test_trips_are_recorded_in_the_tamper_evident_log(tmp_path):
    from agent_saga.integrity import verify

    wal = FileWAL(tmp_path / "w.jsonl")
    await wal.start()
    b = CircuitBreaker(BreakerPolicy(failure_threshold=2, cool_down=0.1), wal=wal)
    for _ in range(2):
        b.record_failure("stripe.charge", "timeout")
    time.sleep(0.15)
    b.check("stripe.charge")
    b.record_success("stripe.charge")
    await wal.barrier()
    records = wal.records()
    await wal.close()

    assert [r["event"] for r in records] == [
        "CIRCUIT_OPEN", "CIRCUIT_HALF_OPEN", "CIRCUIT_CLOSED"]
    assert records[0]["key"] == "stripe.charge"
    assert verify(records).intact


def test_no_breaker_installed_means_no_overhead():
    set_breaker(None)
    assert get_breaker() is None


@aio
async def test_a_saga_runs_normally_with_no_breaker_installed(tmp_path):
    set_breaker(None)
    wal = FileWAL(tmp_path / "w.jsonl")
    await wal.start()
    ctx = SagaContext(gate=PreFlightGate(rules=[]), wal=wal)
    await ctx.begin()
    assert await run_step(ctx) == {"id": "ch_1"}
    await wal.close()
