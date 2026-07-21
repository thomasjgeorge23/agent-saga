"""Invariants that a naive 'AI undo' implementation gets wrong."""

import asyncio

import pytest

from agent_saga import (
    ActionSemantics,
    AsyncWAL,
    Compensation,
    PreFlightGate,
    PreFlightViolation,
    Rule,
    SagaAborted,
    SagaContext,
    StepState,
    Verdict,
    arg_exceeds,
    saga,
    semantics_is,
    tool,
)
from conftest import aio

R = ActionSemantics.REVERSIBLE
C = ActionSemantics.COMPENSABLE
I = ActionSemantics.IRREVERSIBLE


async def _wal(tmp_path=None):
    w = AsyncWAL(tmp_path / "wal.jsonl" if tmp_path else None)
    await w.start()
    return w


# --------------------------------------------------------------------------
# 1. LIFO ordering
# --------------------------------------------------------------------------

@aio
async def test_compensations_run_last_in_first_out():
    order = []
    w = await _wal()
    ctx = SagaContext(wal=w)

    for i in range(3):
        await ctx.execute(
            tool=f"step{i}",
            semantics=C,
            forward=lambda i=i: order.append(f"do{i}"),
            compensate=lambda _r, i=i: Compensation(fn=lambda i=i: order.append(f"undo{i}")),
        )

    report = await ctx.rollback()
    await w.close()

    assert order == ["do0", "do1", "do2", "undo2", "undo1", "undo0"]
    assert report.clean


# --------------------------------------------------------------------------
# 2. Runtime-derived compensation -- the Temporal wedge
# --------------------------------------------------------------------------

@aio
async def test_compensation_is_derived_from_the_forward_result():
    """The agent chose the action; the charge id only exists after it ran.
    A statically declared workflow cannot express this."""
    refunded = []
    w = await _wal()
    ctx = SagaContext(wal=w)

    def charge(amount):
        return {"charge_id": "ch_live_9f2", "amount": amount}

    await ctx.execute(
        tool="stripe.charge",
        semantics=C,
        forward=charge,
        forward_kwargs={"amount": 4200},
        compensate=lambda result: Compensation(
            fn=lambda charge_id: refunded.append(charge_id),
            kwargs={"charge_id": result["charge_id"]},
            description=f"refund {result['charge_id']}",
        ),
    )
    await ctx.rollback()
    await w.close()

    assert refunded == ["ch_live_9f2"]


# --------------------------------------------------------------------------
# 3. Write-ahead ordering -- intent is durable BEFORE the effect
# --------------------------------------------------------------------------

@aio
async def test_intent_is_on_disk_before_the_side_effect_runs(tmp_path=None):
    """The bug in the naive design: registering compensation *after* the
    forward call means a crash in between orphans a real side effect with no
    record it was ever attempted."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        w = AsyncWAL(Path(d) / "wal.jsonl")
        await w.start()
        ctx = SagaContext(wal=w)
        seen = {}

        def effect():
            # Read what is durable on disk at the moment the effect fires.
            seen["records"] = [r["event"] for r in w.records()]
            return "ok"

        await ctx.execute(tool="stripe.charge", semantics=C, forward=effect,
                          compensate=lambda r: Compensation(fn=lambda: None))
        await w.close()

        assert "STEP_INTENT" in seen["records"], (
            "intent must be fsynced before the effect executes"
        )


@aio
async def test_reversible_steps_never_block_on_fsync():
    """Tiered durability: the fast path must not pay for a disk round trip.

    Note this asserts on `barriers`, not on disk contents. The flusher may well
    have written the record opportunistically -- that is fine and desirable.
    The invariant is that we did not *wait* for it.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        w = AsyncWAL(Path(d) / "wal.jsonl")
        await w.start()
        ctx = SagaContext(wal=w)

        comp = lambda r: Compensation(fn=lambda: None)
        await ctx.execute(tool="cache.set", semantics=R, forward=lambda: "ok", compensate=comp)
        assert w.barriers == 0

        # Two barriers on the money path: the intent before the effect, and the
        # compensation descriptor after it (see SagaContext.durable_commit).
        await ctx.execute(tool="stripe.charge", semantics=C, forward=lambda: "ok", compensate=comp)
        assert w.barriers == 2, "money-path steps must be durable before AND after"

        await ctx.execute(tool="cache.set", semantics=R, forward=lambda: "ok", compensate=comp)
        assert w.barriers == 2, "reversible steps must never add a barrier"

        await w.close()


# --------------------------------------------------------------------------
# 4. Pre-flight gate
# --------------------------------------------------------------------------

@aio
async def test_gate_blocks_irreversible_before_any_effect_occurs():
    fired = []
    w = await _wal()
    ctx = SagaContext(wal=w)

    with pytest.raises(PreFlightViolation):
        await ctx.execute(tool="send_email", semantics=I,
                          forward=lambda: fired.append("sent"))

    await w.close()
    assert fired == [], "the gate must refuse before the effect, not after"
    assert ctx.stack == []


@aio
async def test_gate_allows_irreversible_with_human_approval():
    fired = []
    gate = PreFlightGate(approval_provider=lambda ctx, rule: True)
    w = await _wal()
    ctx = SagaContext(gate=gate, wal=w)

    await ctx.execute(tool="send_email", semantics=I, forward=lambda: fired.append("sent"))
    await w.close()
    assert fired == ["sent"]


@aio
async def test_gate_escalates_on_argument_threshold():
    """`transfer(amount=5)` and `transfer(amount=5_000_000)` share a tool name
    and share nothing else."""
    gate = PreFlightGate(
        rules=[Rule("large-transfer", arg_exceeds("amount", 10_000), Verdict.BLOCK,
                    "Transfer exceeds the autonomous limit.")]
    )
    w = await _wal()
    ctx = SagaContext(gate=gate, wal=w)

    assert await ctx.execute(tool="transfer", semantics=C, forward=lambda amount: "ok",
                             forward_kwargs={"amount": 500}) == "ok"

    with pytest.raises(PreFlightViolation):
        await ctx.execute(tool="transfer", semantics=C, forward=lambda amount: "ok",
                          forward_kwargs={"amount": 5_000_000})
    await w.close()


@aio
async def test_gate_fails_closed_when_a_predicate_raises():
    def broken(ctx):
        raise ValueError("policy service unreachable")

    gate = PreFlightGate(rules=[Rule("broken", broken, Verdict.ALLOW)])
    w = await _wal()
    ctx = SagaContext(gate=gate, wal=w)
    fired = []

    with pytest.raises(PreFlightViolation):
        await ctx.execute(tool="t", semantics=C, forward=lambda: fired.append(1))
    await w.close()
    assert fired == []


# --------------------------------------------------------------------------
# 5. UNKNOWN outcomes -- the timed-out charge
# --------------------------------------------------------------------------

@aio
async def test_failed_forward_call_is_unknown_not_absent():
    """A timed-out POST to Stripe may well have charged the card. Treating the
    failure as 'it did not happen' is how you leave money on the floor."""
    compensated = []
    w = await _wal()
    ctx = SagaContext(wal=w)

    def flaky():
        raise TimeoutError("upstream timeout")

    with pytest.raises(TimeoutError):
        await ctx.execute(
            tool="stripe.charge", semantics=C, forward=flaky,
            compensate=lambda result: Compensation(
                fn=lambda: compensated.append("refund-attempted"),
                description="idempotent refund by key",
            ),
        )

    assert ctx.stack[0].state is StepState.UNKNOWN
    report = await ctx.rollback()
    await w.close()

    assert compensated == ["refund-attempted"]
    assert report.clean


# --------------------------------------------------------------------------
# 6. Honest reporting of what could not be undone
# --------------------------------------------------------------------------

@aio
async def test_irreversible_effect_is_reported_as_orphaned_not_silently_skipped():
    gate = PreFlightGate(approval_provider=lambda ctx, rule: True)
    w = await _wal()
    ctx = SagaContext(gate=gate, wal=w)

    await ctx.execute(tool="send_email", semantics=I, forward=lambda: "sent")
    report = await ctx.rollback()
    await w.close()

    assert not report.clean
    assert [s.tool for s in report.orphaned] == ["send_email"]
    assert report.orphaned[0].state is StepState.ORPHANED
    assert "send_email" in report.summary()


@aio
async def test_compensation_failure_halts_and_marks_remaining_unresolved():
    """If step N's compensation fails, step N-1's may operate on state it no
    longer understands. Halting is the safe default."""
    ran = []
    w = await _wal()
    ctx = SagaContext(wal=w, halt_on_compensation_failure=True)

    await ctx.execute(tool="first", semantics=C, forward=lambda: None,
                      compensate=lambda r: Compensation(fn=lambda: ran.append("undo-first")))
    await ctx.execute(tool="second", semantics=C, forward=lambda: None,
                      compensate=lambda r: Compensation(fn=_boom))

    report = await ctx.rollback()
    await w.close()

    assert ran == [], "must not continue compensating past a failure"
    assert [s.tool for s in report.failed] == ["second"]
    assert [s.tool for s in report.unresolved] == ["first"]
    assert report.halted and not report.clean


def _boom():
    raise RuntimeError("Salesforce API 503")


@aio
async def test_compensation_failure_can_continue_when_explicitly_opted_in():
    ran = []
    w = await _wal()
    ctx = SagaContext(wal=w, halt_on_compensation_failure=False)

    await ctx.execute(tool="first", semantics=C, forward=lambda: None,
                      compensate=lambda r: Compensation(fn=lambda: ran.append("undo-first")))
    await ctx.execute(tool="second", semantics=C, forward=lambda: None,
                      compensate=lambda r: Compensation(fn=_boom))

    report = await ctx.rollback()
    await w.close()
    assert ran == ["undo-first"]
    assert len(report.failed) == 1 and len(report.compensated) == 1


@aio
async def test_missing_compensation_factory_is_orphaned_not_assumed_clean():
    w = await _wal()
    ctx = SagaContext(wal=w)
    await ctx.execute(tool="crm.update", semantics=C, forward=lambda: "ok")
    report = await ctx.rollback()
    await w.close()
    assert [s.tool for s in report.orphaned] == ["crm.update"]
    assert not report.clean


@aio
async def test_broken_compensation_factory_does_not_mask_the_original_failure():
    w = await _wal()
    ctx = SagaContext(wal=w)

    def bad_factory(result):
        raise KeyError("charge_id")

    await ctx.execute(tool="t", semantics=C, forward=lambda: {}, compensate=bad_factory)
    report = await ctx.rollback()
    await w.close()
    assert [s.tool for s in report.orphaned] == ["t"]


# --------------------------------------------------------------------------
# 7. The @saga / @tool boundary
# --------------------------------------------------------------------------

@aio
async def test_saga_decorator_rolls_back_on_exception_and_carries_the_report():
    log = []

    @tool(semantics=C, compensate=lambda r: Compensation(fn=lambda: log.append("undo-crm")))
    def update_crm(record_id):
        log.append("do-crm")
        return {"record_id": record_id}

    @saga
    async def agent_run():
        await update_crm(record_id="acct_1")
        raise ValueError("model hallucinated a field")

    with pytest.raises(SagaAborted) as exc:
        await agent_run()

    assert log == ["do-crm", "undo-crm"]
    assert isinstance(exc.value.cause, ValueError)
    assert exc.value.report.clean


@aio
async def test_saga_records_the_abort_cause_before_rollback():
    """The triggering exception is only known at the boundary; it is written to
    the WAL there so a post-mortem can name the failure, not just see that one
    happened."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        w = AsyncWAL(Path(d) / "wal.jsonl")
        await w.start()

        @tool(semantics=C, compensate=lambda r: Compensation(fn=lambda: None, handler="h"))
        def act():
            return 1

        @saga(wal=w, reraise=False)
        async def run():
            await act()
            raise ValueError("model hallucinated a field")

        await run()
        await w.close()

        recs = {r["event"]: r for r in w.records()}
        assert recs["SAGA_ABORT_CAUSE"]["cause_type"] == "ValueError"
        assert "hallucinated" in recs["SAGA_ABORT_CAUSE"]["cause"]

        events = [r["event"] for r in w.records()]
        assert events.index("SAGA_ABORT_CAUSE") < events.index("ROLLBACK_START")


@aio
async def test_successful_saga_records_no_abort_cause():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        w = AsyncWAL(Path(d) / "wal.jsonl")
        await w.start()

        @saga(wal=w)
        async def run():
            return "ok"

        assert await run() == "ok"
        await w.close()
        assert "SAGA_ABORT_CAUSE" not in {r["event"] for r in w.records()}


@aio
async def test_tool_passes_through_outside_a_saga_boundary():
    @tool(semantics=C, compensate=lambda r: Compensation(fn=lambda: None))
    def touch():
        return "ran"

    assert await touch() == "ran"


@aio
async def test_concurrent_sagas_do_not_share_a_compensation_stack():
    """contextvars, not a module global -- one process runs many agents."""
    log = []

    @tool(semantics=C, compensate=lambda r: Compensation(fn=lambda: log.append(f"undo-{r}")))
    async def act(name):
        await asyncio.sleep(0.01)
        return name

    @saga(reraise=False)
    async def failing(name):
        await act(name=name)
        raise ValueError("boom")

    @saga(reraise=False)
    async def succeeding(name):
        await act(name=name)

    await asyncio.gather(failing("a"), succeeding("b"))
    assert log == ["undo-a"], "the successful saga must not compensate"


@aio
async def test_saga_rejects_sync_functions():
    with pytest.raises(TypeError):
        @saga
        def not_async():
            pass


@aio
async def test_rollback_cannot_run_twice():
    w = await _wal()
    ctx = SagaContext(wal=w)
    await ctx.rollback()
    with pytest.raises(RuntimeError):
        await ctx.rollback()
    await w.close()


# --------------------------------------------------------------------------
# 8. Idempotency
# --------------------------------------------------------------------------

@aio
async def test_compensation_carries_a_stable_idempotency_key():
    """Compensation may be retried, or run after an UNKNOWN forward call.
    Without a key the connector double-refunds."""
    w = await _wal()
    ctx = SagaContext(wal=w)
    await ctx.execute(tool="t", semantics=C, forward=lambda: "r",
                      compensate=lambda r: Compensation(fn=lambda: None, idempotency_key="fixed-key"))
    step = ctx.stack[0]
    assert step.compensation.idempotency_key == "fixed-key"
    assert step.compensation.describe()["idempotency_key"] == "fixed-key"
    await w.close()


# --------------------------------------------------------------------------
# 9. Event loop hygiene
# --------------------------------------------------------------------------

@aio
async def test_blocking_tool_does_not_stall_the_event_loop():
    import time

    w = await _wal()
    ctx = SagaContext(wal=w)
    ticks = []

    async def heartbeat():
        for _ in range(8):
            await asyncio.sleep(0.01)
            ticks.append(1)

    hb = asyncio.create_task(heartbeat())
    await ctx.execute(tool="blocking_http", semantics=C,
                      forward=lambda: time.sleep(0.08),
                      compensate=lambda r: Compensation(fn=lambda: None))
    await hb
    await w.close()
    assert len(ticks) >= 5, "sync tools must run off the event loop"


@aio
async def test_timeout_marks_the_step_unknown():
    w = await _wal()
    ctx = SagaContext(wal=w)

    async def slow():
        await asyncio.sleep(5)

    with pytest.raises(asyncio.TimeoutError):
        await ctx.execute(tool="slow", semantics=C, forward=slow, timeout=0.05,
                          compensate=lambda r: Compensation(fn=lambda: None))
    assert ctx.stack[0].state is StepState.UNKNOWN
    await w.close()


# --------------------------------------------------------------------------
# 10. WAL
# --------------------------------------------------------------------------

@aio
async def test_wal_records_a_replayable_ordered_trace():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        w = AsyncWAL(Path(d) / "wal.jsonl")
        await w.start()
        ctx = SagaContext(wal=w)
        await ctx.execute(tool="a", semantics=C, forward=lambda: "x",
                          compensate=lambda r: Compensation(fn=lambda: None))
        await ctx.rollback()
        await w.close()

        events = [r["event"] for r in w.records()]
        assert events == ["STEP_INTENT", "STEP_COMMITTED", "ROLLBACK_START",
                          "COMPENSATED", "ROLLBACK_END"]
        seqs = [r["seq"] for r in w.records()]
        assert seqs == sorted(seqs)


@aio
async def test_wal_drop_silent_sheds_load_and_returns_a_sentinel():
    """DROP_SILENT is opt-in, for low-value work only. It must return the DROPPED
    sentinel, never a real seq -- a dropped record that reported a valid seq
    would fool barrier() into calling it durable."""
    from agent_saga import BackpressurePolicy
    from agent_saga.wal import DROPPED

    w = AsyncWAL(max_buffer=10, backpressure=BackpressurePolicy.DROP_SILENT)
    await w.start()
    results = [w.append("NOISE", {"i": i}) for i in range(50)]
    assert w.dropped > 0
    assert DROPPED in results
    assert all(r == DROPPED or r > 0 for r in results)
    await w.close()


@aio
async def test_wal_default_policy_raises_rather_than_silently_dropping():
    """The safe default: refuse to proceed without a durable record. The intent
    append happens before the side effect, so raising here aborts the step with
    nothing executed."""
    from agent_saga import WALBackpressure

    w = AsyncWAL(max_buffer=10)   # default backpressure = RAISE
    await w.start()
    with pytest.raises(WALBackpressure):
        for i in range(50):
            w.append("NOISE", {"i": i})
    await w.close()


@aio
async def test_wal_block_policy_never_drops_and_ensure_capacity_drains():
    from agent_saga import BackpressurePolicy

    w = AsyncWAL(max_buffer=10, backpressure=BackpressurePolicy.BLOCK)
    await w.start()
    for i in range(50):
        w.append("NOISE", {"i": i})   # never raises, never drops
    assert w.dropped == 0
    await w.ensure_capacity()          # yields until the flusher drains the buffer
    assert len(w._buf) < w._max_buffer
    await w.close()
