"""Surgical repair: keep the good steps, fix the bad one, finish the job.

The feature is an escape hatch inside a guarantee, so most of these tests are
about the ways it must refuse:

1. It resumes only when every retained step could still be undone from the log
   alone -- otherwise repairing manufactures the orphan the engine exists to
   prevent.
2. A failure after the repair unwinds the WHOLE transaction, including the
   steps that were kept. That is the difference between resuming a transaction
   and starting a second one that happens to run afterwards.
3. Operator and reason are mandatory, and every action is on the log. An
   unaudited hatch voids the audit it sits inside.
"""

import pytest
from conftest import aio

from agent_saga import ActionSemantics, AsyncWAL, Compensation, SagaAborted, saga_scope
from agent_saga.registry import compensator
from agent_saga.repair import RepairBlocked, RepairSession

C = ActionSemantics.COMPENSABLE

WORLD = {"accounts": [], "charges": [], "welcome": []}


@compensator("repairtest.delete_account")
def delete_account(account_id: str):
    WORLD["accounts"] = [a for a in WORLD["accounts"] if a != account_id]


@compensator("repairtest.refund")
def refund(charge_id: str):
    WORLD["charges"] = [c for c in WORLD["charges"] if c != charge_id]


@pytest.fixture(autouse=True)
def _clean():
    for key in WORLD:
        WORLD[key].clear()
    yield
    for key in WORLD:
        WORLD[key].clear()


def create_account(name):
    WORLD["accounts"].append("acct_1")
    return {"id": "acct_1", "name": name}


def charge_card(amount):
    WORLD["charges"].append("ch_1")
    return {"id": "ch_1", "amount": amount}


async def run_failing_saga(wal, *, recoverable=True, postcode="!!bad!!"):
    """Two good steps, then one that fails on a malformed argument."""
    def account_comp(r):
        if recoverable:
            return Compensation(fn=delete_account, handler="repairtest.delete_account",
                                kwargs={"account_id": r["id"]}, description="delete")
        # an in-process closure: fine in this process, useless to any other
        return Compensation(fn=lambda: WORLD["accounts"].clear(), description="delete")

    with pytest.raises(SagaAborted):
        async with saga_scope(wal=wal, name="onboarding") as saga:
            await saga.execute(tool="crm.create_account", semantics=C,
                               forward=create_account, forward_kwargs={"name": "Ada"},
                               compensate=account_comp)
            await saga.execute(tool="stripe.charge", semantics=C,
                               forward=charge_card, forward_kwargs={"amount": 4200},
                               compensate=lambda r: Compensation(
                                   fn=refund, handler="repairtest.refund",
                                   kwargs={"charge_id": r["id"]}, description="refund"))

            def ship(postcode):
                raise ValueError(f"malformed postcode: {postcode}")

            await saga.execute(tool="ship.schedule", semantics=C,
                               forward=ship, forward_kwargs={"postcode": postcode},
                               compensate=lambda r: Compensation(
                                   fn=lambda: None, description="unschedule"))


# -- opening a session ------------------------------------------------------------

@aio
async def test_a_session_reconstructs_what_was_kept_and_what_failed(tmp_path):
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        await run_failing_saga(wal)
        records = await wal.read_all()
    finally:
        await wal.close()

    saga_id = next(r["saga_id"] for r in records if r["event"] == "SAGA_START")
    session = RepairSession.open(records, saga_id, operator="ops@x.com",
                                 reason="malformed postcode")

    assert [s.tool for s in session.retained] == ["crm.create_account", "stripe.charge"]
    assert session.failure.tool == "ship.schedule"
    assert session.args == {"postcode": "!!bad!!"}


def test_operator_and_reason_are_mandatory():
    records = [{"event": "SAGA_START", "saga_id": "s1"}]
    with pytest.raises(ValueError, match="operator is required"):
        RepairSession.open(records, "s1", operator="", reason="x")
    with pytest.raises(ValueError, match="reason is required"):
        RepairSession.open(records, "s1", operator="ops@x.com", reason="  ")


def test_an_unknown_saga_is_a_lookup_error():
    with pytest.raises(LookupError, match="no records"):
        RepairSession.open([], "nope", operator="ops@x.com", reason="x")


# -- 1. the precondition that keeps it safe ------------------------------------------

@aio
async def test_resume_is_refused_when_a_retained_step_cannot_be_undone(tmp_path):
    """The heart of it. Keeping a step whose inverse is an in-process closure
    means that if the resumed part fails, that step can never be rolled back --
    repairing would have manufactured the orphan."""
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        await run_failing_saga(wal, recoverable=False)
        records = await wal.read_all()
    finally:
        await wal.close()

    saga_id = next(r["saga_id"] for r in records if r["event"] == "SAGA_START")
    session = RepairSession.open(records, saga_id, operator="ops@x.com", reason="x")

    assert not session.can_resume
    blocker = "\n".join(session.blockers)
    assert "no registry-backed compensation" in blocker
    assert "crm.create_account" in blocker
    assert "!!" in session.format_text()

    async def continuation(ctx):
        return "should not run"

    with pytest.raises(RepairBlocked, match="registry-backed"):
        await session.resume(continuation, wal=None)


@aio
async def test_an_already_rolled_back_saga_cannot_be_resumed(tmp_path):
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        await run_failing_saga(wal)
        records = await wal.read_all()
    finally:
        await wal.close()

    saga_id = next(r["saga_id"] for r in records if r["event"] == "SAGA_START")
    session = RepairSession.open(records, saga_id, operator="ops@x.com", reason="x")
    # the original saga aborted and unwound, so its effects are gone
    assert any("already rolled back" in b for b in session.blockers)


# -- the happy path: fix and finish ----------------------------------------------------

@aio
async def test_amend_then_resume_keeps_the_good_steps_and_finishes(tmp_path):
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        await run_failing_saga(wal)
        records = await wal.read_all()

        saga_id = next(r["saga_id"] for r in records if r["event"] == "SAGA_START")
        session = RepairSession.open(records, saga_id, operator="ops@x.com",
                                     reason="malformed postcode from the model")

        # the rollback already ran in this fixture, so re-create the kept state
        # and clear the terminal blocker to exercise the resume path
        WORLD["accounts"].append("acct_1")
        WORLD["charges"].append("ch_1")
        session.terminal = None

        session.amend(postcode="SW1A 1AA")
        assert session.can_resume, session.format_text()

        async def continuation(ctx):
            def ship(postcode):
                WORLD["welcome"].append(postcode)
                return {"id": "shp_1"}

            return await ctx.execute(
                tool="ship.schedule", semantics=C, forward=ship,
                forward_kwargs=session.args,
                compensate=lambda r: Compensation(fn=lambda: None, description="x"))

        result = await session.resume(continuation, wal=wal)
        assert result == {"id": "shp_1"}
        after = await wal.read_all()
    finally:
        await wal.close()

    assert WORLD["welcome"] == ["SW1A 1AA"]
    assert WORLD["accounts"] == ["acct_1"]      # kept, not rolled back
    assert WORLD["charges"] == ["ch_1"]

    events = [r["event"] for r in after]
    assert "REPAIR_OPENED" in events and "REPAIR_RESUMED" in events


# -- 2. a failure after the repair unwinds everything --------------------------------------

@aio
async def test_a_failure_after_resuming_rolls_back_the_retained_steps_too(tmp_path):
    """Otherwise a repair would leave the pre-repair steps stranded with nobody
    owning them -- a half-transaction created deliberately."""
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        await run_failing_saga(wal)
        records = await wal.read_all()

        saga_id = next(r["saga_id"] for r in records if r["event"] == "SAGA_START")
        session = RepairSession.open(records, saga_id, operator="ops@x.com", reason="x")
        WORLD["accounts"].append("acct_1")
        WORLD["charges"].append("ch_1")
        session.terminal = None
        session.amend(postcode="SW1A 1AA")

        async def continuation(ctx):
            def boom(**kw):
                raise RuntimeError("carrier API down")
            await ctx.execute(tool="ship.schedule", semantics=C, forward=boom,
                              forward_kwargs=session.args,
                              compensate=lambda r: Compensation(
                                  fn=lambda: None, description="x"))

        with pytest.raises(SagaAborted) as excinfo:
            await session.resume(continuation, wal=wal)
    finally:
        await wal.close()

    # the INHERITED steps were compensated, not just the resumed one
    compensated = [s.tool for s in excinfo.value.report.compensated]
    assert "stripe.charge" in compensated
    assert "crm.create_account" in compensated
    assert WORLD["accounts"] == [] and WORLD["charges"] == []


@aio
async def test_repairing_a_crashed_saga_needs_no_test_hack(tmp_path):
    """The realistic entry point, with nothing mutated by hand.

    A saga whose process died has no terminal record and its effects are still
    standing -- which is exactly when repairing beats rolling back. (The other
    real entry point is `agent-saga quarantine`, which freezes a saga
    deliberately without unwinding it.) The tests above simulate this by
    clearing `terminal`; this one earns it.
    """
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        # A saga that commits two steps and then "dies": no rollback, no
        # terminal event, effects left in the world.
        async with saga_scope(wal=wal, name="crashed") as saga:
            await saga.execute(tool="crm.create_account", semantics=C,
                               forward=create_account, forward_kwargs={"name": "Ada"},
                               compensate=lambda r: Compensation(
                                   fn=delete_account, handler="repairtest.delete_account",
                                   kwargs={"account_id": r["id"]}, description="delete"))
            await saga.execute(tool="stripe.charge", semantics=C,
                               forward=charge_card, forward_kwargs={"amount": 4200},
                               compensate=lambda r: Compensation(
                                   fn=refund, handler="repairtest.refund",
                                   kwargs={"charge_id": r["id"]}, description="refund"))
        records = [r for r in await wal.read_all()
                   if r.get("event") not in ("SAGA_COMPLETE", "SAGA_ABORTED")]

        saga_id = next(r["saga_id"] for r in records if r["event"] == "SAGA_START")
        session = RepairSession.open(records, saga_id, operator="ops@x.com",
                                     reason="process died mid-onboarding")

        assert session.can_resume, session.format_text()
        assert [s.tool for s in session.retained] == ["crm.create_account",
                                                      "stripe.charge"]

        async def continuation(ctx):
            def ship(postcode):
                WORLD["welcome"].append(postcode)
                return {"id": "shp_1"}
            return await ctx.execute(
                tool="ship.schedule", semantics=C, forward=ship,
                forward_kwargs={"postcode": "SW1A 1AA"},
                compensate=lambda r: Compensation(fn=lambda: None, description="x"))

        assert await session.resume(continuation, wal=wal) == {"id": "shp_1"}
    finally:
        await wal.close()

    assert WORLD["welcome"] == ["SW1A 1AA"]
    assert WORLD["accounts"] == ["acct_1"]      # the crashed saga's work is kept
    assert WORLD["charges"] == ["ch_1"]


# -- 3. everything is on the log --------------------------------------------------------

@aio
async def test_amendments_record_the_before_value(tmp_path):
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        await run_failing_saga(wal)
        records = await wal.read_all()
    finally:
        await wal.close()

    saga_id = next(r["saga_id"] for r in records if r["event"] == "SAGA_START")
    session = RepairSession.open(records, saga_id, operator="ops@x.com", reason="x")
    session.amend(postcode="SW1A 1AA")

    amendment = session.describe()["amendments"][0]
    assert amendment["field"] == "postcode"
    assert amendment["from"] == "!!bad!!"        # "changed 50 to 5000" must be visible
    assert amendment["to"] == "SW1A 1AA"


def test_a_manual_resolution_needs_a_note():
    records = [{"event": "SAGA_START", "saga_id": "s1"}]
    session = RepairSession.open(records, "s1", operator="ops@x.com", reason="x")
    with pytest.raises(ValueError, match="note is required"):
        session.mark_resolved("   ")
    session.mark_resolved("drained the stuck queue by hand")
    assert session.describe()["manual_notes"] == ["drained the stuck queue by hand"]


@aio
async def test_abandoning_is_recorded_and_does_not_roll_anything_back(tmp_path):
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        await run_failing_saga(wal)
        records = await wal.read_all()
        saga_id = next(r["saga_id"] for r in records if r["event"] == "SAGA_START")
        session = RepairSession.open(records, saga_id, operator="ops@x.com",
                                     reason="not worth repairing")
        payload = session.abandon(wal)
        await wal.barrier()
        after = await wal.read_all()
    finally:
        await wal.close()

    assert payload["decision"] == "abandoned"
    assert any(r["event"] == "REPAIR_ABANDONED" for r in after)
    assert not session.can_resume               # the session is spent


@aio
async def test_a_session_cannot_be_used_twice(tmp_path):
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        await run_failing_saga(wal)
        records = await wal.read_all()
        saga_id = next(r["saga_id"] for r in records if r["event"] == "SAGA_START")
        session = RepairSession.open(records, saga_id, operator="ops@x.com", reason="x")
        session.abandon(wal)
        assert any("already been resumed or abandoned" in b for b in session.blockers)
    finally:
        await wal.close()
