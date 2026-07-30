"""Counterfactual replay: evaluate a candidate against recorded history.

Two properties matter more than the feature itself:

1. **Nothing executes.** A replay that could fire a side effect would be worse
   than no replay -- it would charge a real card to answer a hypothetical. The
   test for this hands the replay a forward callable that raises if touched.
2. **A divergence is UNKNOWABLE, not a verdict.** The moment a candidate calls
   something the recording has no result for, the log genuinely cannot say what
   would have happened. Reporting anything else would be a guess dressed as a
   measurement, and it is how offline evaluation gets a bad name.
"""

import pytest
from conftest import aio

from agent_saga import ActionSemantics, AsyncWAL, Compensation, saga_scope
from agent_saga.counterfactual import counterfactual_replay

C = ActionSemantics.COMPENSABLE


async def record_history(wal) -> str:
    """A real two-step saga, so the recording is a real recording."""
    def charge(amount):
        return {"id": "ch_1", "amount": amount}

    def ship(sku, qty):
        return {"id": "shp_1"}

    async with saga_scope(wal=wal, name="orders") as saga:
        await saga.execute(tool="stripe.charge", semantics=C, forward=charge,
                           forward_kwargs={"amount": 4200},
                           compensate=lambda r: Compensation(
                               fn=lambda: None, description="refund"))
        await saga.execute(tool="ship.order", semantics=C, forward=ship,
                           forward_kwargs={"sku": "widget", "qty": 2},
                           compensate=lambda r: Compensation(
                               fn=lambda: None, description="unship"))
        return saga.saga_id


@pytest.fixture
async def recording(tmp_path):
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        saga_id = await record_history(wal)
        return saga_id, await wal.read_all()
    finally:
        await wal.close()


def boom(**kwargs):
    raise AssertionError(
        "a forward callable was invoked during a replay -- the whole point is "
        "that nothing executes")


# -- 1. nothing executes ------------------------------------------------------------

@aio
async def test_no_forward_callable_is_ever_invoked(tmp_path):
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        saga_id = await record_history(wal)
        records = await wal.read_all()
    finally:
        await wal.close()

    async def candidate(ctx):
        # `boom` raises if called. It is not called.
        await ctx.execute(tool="stripe.charge", semantics=C, forward=boom,
                          forward_kwargs={"amount": 4200})
        await ctx.execute(tool="ship.order", semantics=C, forward=boom,
                          forward_kwargs={"sku": "widget", "qty": 2})

    verdict = await counterfactual_replay(records, saga_id, candidate)
    assert verdict.faithful, verdict.format_text()
    assert verdict.candidate_error is None


# -- the faithful case ---------------------------------------------------------------

@aio
async def test_a_candidate_that_reproduces_the_path_is_faithful(tmp_path):
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        saga_id = await record_history(wal)
        records = await wal.read_all()
    finally:
        await wal.close()

    async def candidate(ctx):
        a = await ctx.execute(tool="stripe.charge", semantics=C, forward=boom,
                              forward_kwargs={"amount": 4200})
        await ctx.execute(tool="ship.order", semantics=C, forward=boom,
                          forward_kwargs={"sku": "widget", "qty": 2})
        return a

    verdict = await counterfactual_replay(records, saga_id, candidate)
    assert verdict.matched == 2
    assert verdict.match_rate == 1.0
    assert verdict.left_the_recording_at is None
    assert "reproduced the recorded path exactly" in verdict.format_text()


# -- 2. divergence is unknowable, and the replay stops -------------------------------

@aio
async def test_different_arguments_are_unknowable_not_wrong(tmp_path):
    """Same tool, different amount. The recording has no result for that call,
    so what would have happened is genuinely unknown."""
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        saga_id = await record_history(wal)
        records = await wal.read_all()
    finally:
        await wal.close()

    async def candidate(ctx):
        await ctx.execute(tool="stripe.charge", semantics=C, forward=boom,
                          forward_kwargs={"amount": 9999})     # <- diverges

    verdict = await counterfactual_replay(records, saga_id, candidate)
    assert not verdict.faithful
    assert verdict.matched == 0
    assert verdict.left_the_recording_at == 1

    divergence = verdict.divergences[0]
    assert divergence.kind == "DIVERGED_ARGS"
    assert not divergence.knowable
    assert "9999" in divergence.detail and "4200" in divergence.detail
    # whitespace-normalised: the report wraps, and the assertion is about what
    # it says, not where the lines happen to break
    report = " ".join(verdict.format_text().split())
    assert "UNKNOWABLE" in report
    assert "verdict on the candidate" in verdict.format_text()


@aio
async def test_a_different_tool_stops_the_replay_at_that_step(tmp_path):
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        saga_id = await record_history(wal)
        records = await wal.read_all()
    finally:
        await wal.close()

    async def candidate(ctx):
        await ctx.execute(tool="stripe.charge", semantics=C, forward=boom,
                          forward_kwargs={"amount": 4200})
        await ctx.execute(tool="email.send", semantics=C, forward=boom,
                          forward_kwargs={"to": "x@y.com"})     # not in the log

    verdict = await counterfactual_replay(records, saga_id, candidate)
    assert verdict.matched == 1                     # the first step did match
    assert verdict.left_the_recording_at == 2
    assert verdict.divergences[-1].kind == "DIVERGED_TOOL"
    assert verdict.divergences[-1].candidate_tool == "email.send"


@aio
async def test_a_candidate_that_stops_early_is_reported(tmp_path):
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        saga_id = await record_history(wal)
        records = await wal.read_all()
    finally:
        await wal.close()

    async def candidate(ctx):
        await ctx.execute(tool="stripe.charge", semantics=C, forward=boom,
                          forward_kwargs={"amount": 4200})
        # and then simply stops, one step short

    verdict = await counterfactual_replay(records, saga_id, candidate)
    assert verdict.matched == 1
    assert not verdict.faithful
    assert verdict.divergences[-1].kind == "MISSING_CALLS"
    assert "stopped after 1 step" in verdict.divergences[-1].detail


@aio
async def test_extra_calls_past_the_end_of_the_recording(tmp_path):
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        saga_id = await record_history(wal)
        records = await wal.read_all()
    finally:
        await wal.close()

    async def candidate(ctx):
        await ctx.execute(tool="stripe.charge", semantics=C, forward=boom,
                          forward_kwargs={"amount": 4200})
        await ctx.execute(tool="ship.order", semantics=C, forward=boom,
                          forward_kwargs={"sku": "widget", "qty": 2})
        await ctx.execute(tool="audit.log", semantics=C, forward=boom,
                          forward_kwargs={})          # one more than recorded

    verdict = await counterfactual_replay(records, saga_id, candidate)
    assert verdict.matched == 2
    assert verdict.divergences[-1].kind == "EXTRA_CALL"
    assert not verdict.faithful


# -- the candidate's own failure is data, not a crash ----------------------------------

@aio
async def test_a_candidate_that_raises_is_recorded_rather_than_propagated(tmp_path):
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        saga_id = await record_history(wal)
        records = await wal.read_all()
    finally:
        await wal.close()

    async def candidate(ctx):
        await ctx.execute(tool="stripe.charge", semantics=C, forward=boom,
                          forward_kwargs={"amount": 4200})
        raise ValueError("the candidate model produced nonsense")

    verdict = await counterfactual_replay(records, saga_id, candidate)
    assert verdict.matched == 1
    assert "produced nonsense" in verdict.candidate_error
    assert not verdict.faithful


# -- an empty recording proves nothing ---------------------------------------------------

@aio
async def test_an_empty_recording_is_not_reported_as_faithful():
    """Vacuous success is the failure mode this package keeps refusing."""
    async def candidate(ctx):
        return None

    verdict = await counterfactual_replay([], "nope", candidate)
    assert verdict.recorded_steps == ()
    assert verdict.faithful is False
    assert verdict.match_rate == 0.0
    assert "nothing was proven" in verdict.format_text()


@aio
async def test_the_verdict_is_machine_readable(tmp_path):
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        saga_id = await record_history(wal)
        records = await wal.read_all()
    finally:
        await wal.close()

    async def candidate(ctx):
        await ctx.execute(tool="stripe.charge", semantics=C, forward=boom,
                          forward_kwargs={"amount": 9999})

    data = (await counterfactual_replay(records, saga_id, candidate)).describe()
    assert data["recorded_steps"] == 2
    assert data["matched"] == 0
    assert data["faithful"] is False
    assert data["left_the_recording_at"] == 1
    assert data["divergences"][0]["knowable"] is False
