"""UMIP (Phase 3.2): one saga spanning several frameworks, unwinding as a unit."""

import pytest
from conftest import aio

from agent_saga import saga_scope
from agent_saga.context import Compensation
from agent_saga.semantics import ActionSemantics as S
from agent_saga.umip import (
    UMIP_VERSION, Participant, UMIPConformanceError, UMIPRegistry,
    check_conformance, get_registry, set_registry)
from agent_saga.wal.file_wal import FileWAL


# -- the protocol's teeth -----------------------------------------------------

def test_compensable_without_compensation_is_refused():
    with pytest.raises(UMIPConformanceError) as exc:
        check_conformance(Participant("t", "fw", S.COMPENSABLE, lambda: None))
    assert "COMPENSABLE" in str(exc.value)


def test_irreversible_with_compensation_is_refused():
    with pytest.raises(UMIPConformanceError) as exc:
        check_conformance(Participant("t", "fw", S.IRREVERSIBLE, lambda: None,
                                      compensate=lambda r: None))
    assert "IRREVERSIBLE" in str(exc.value)


def test_reversible_needs_no_compensation():
    check_conformance(Participant("t", "fw", S.REVERSIBLE, lambda: None))


def test_bad_participants_are_refused():
    for bad in (
        Participant("", "fw", S.REVERSIBLE, lambda: None),          # no name
        Participant("t", "fw", S.REVERSIBLE, "not callable"),       # not callable
        Participant("t", "fw", "REVERSIBLE", lambda: None),         # wrong type
    ):
        with pytest.raises(UMIPConformanceError):
            check_conformance(bad)


def test_duplicate_names_are_refused():
    reg = UMIPRegistry()
    reg.register(Participant("dup", "a", S.REVERSIBLE, lambda: 1))
    with pytest.raises(UMIPConformanceError) as exc:
        reg.register(Participant("dup", "b", S.REVERSIBLE, lambda: 2))
    assert "already registered" in str(exc.value)


# -- the point: one saga, several frameworks ----------------------------------

def _cross_framework_registry(world, undone):
    async def book_van(when):                      # async, "LangChain-style"
        world["van"] = f"van@{when}"
        return {"booking_id": "bk_1"}

    def undo_van(res):
        return Compensation(
            fn=lambda **k: (world.update(van=None), undone.append("van")),
            handler="van.cancel", kwargs={"booking_id": res["booking_id"]})

    def charge_card(amount):                       # SYNC, "CrewAI-style"
        world["charge"] = amount
        return {"charge_id": "ch_9"}

    def undo_charge(res):
        return Compensation(
            fn=lambda **k: (world.update(charge=None), undone.append("charge")),
            handler="card.refund", kwargs={"charge_id": res["charge_id"]})

    reg = UMIPRegistry()
    reg.register(Participant("van.book", "langchain", S.COMPENSABLE, book_van, undo_van))
    reg.register(Participant("card.charge", "crewai", S.COMPENSABLE, charge_card, undo_charge))
    return reg


@aio
async def test_failure_in_one_framework_unwinds_the_others():
    world = {"van": None, "charge": None}
    undone = []
    reg = _cross_framework_registry(world, undone)

    wal = FileWAL()
    await wal.start()
    with pytest.raises(Exception):
        async with saga_scope(name="job-42", wal=wal):
            await reg.invoke("van.book", when="tuesday")
            await reg.invoke("card.charge", amount=8000)
            assert world == {"van": "van@tuesday", "charge": 8000}
            raise RuntimeError("permit refused")       # a third system says no
    await wal.close()

    # both frameworks' effects unwound, newest first
    assert world == {"van": None, "charge": None}
    assert undone == ["charge", "van"]


@aio
async def test_sync_participant_runs_without_blocking_the_loop():
    import asyncio
    reg = UMIPRegistry()
    reg.register(Participant("slow.sync", "crewai", S.REVERSIBLE,
                             lambda: __import__("time").sleep(0.05) or "done"))
    wal = FileWAL()
    await wal.start()
    async with saga_scope(wal=wal):
        # a concurrent task must still make progress while the sync tool blocks
        ticks = {"n": 0}

        async def ticker():
            for _ in range(5):
                await asyncio.sleep(0.005)
                ticks["n"] += 1

        t = asyncio.create_task(ticker())
        assert await reg.invoke("slow.sync") == "done"
        await t
    await wal.close()
    assert ticks["n"] == 5          # the loop kept running


@aio
async def test_participant_outside_a_saga_calls_through():
    reg = UMIPRegistry()
    reg.register(Participant("plain", "local", S.REVERSIBLE, lambda x: x * 2))
    assert await reg.invoke("plain", x=21) == 42       # no saga, no error


@aio
async def test_unknown_participant_names_the_registered_ones():
    reg = UMIPRegistry()
    reg.register(Participant("known", "local", S.REVERSIBLE, lambda: 1))
    with pytest.raises(KeyError) as exc:
        await reg.invoke("missing")
    assert "known" in str(exc.value)


# -- introspection ------------------------------------------------------------

def test_manifest_describes_what_can_interoperate():
    reg = _cross_framework_registry({}, [])
    m = reg.manifest()
    assert m["umip_version"] == UMIP_VERSION
    assert m["frameworks"] == ["crewai", "langchain"]
    names = {p["name"] for p in m["participants"]}
    assert names == {"van.book", "card.charge"}
    assert all(p["compensating"] for p in m["participants"])


def test_registry_container_protocol():
    reg = _cross_framework_registry({}, [])
    assert len(reg) == 2 and "van.book" in reg
    assert {p.name for p in reg} == {"van.book", "card.charge"}
    assert reg.get("van.book").framework == "langchain"
    assert reg.get("nope") is None


def test_decorator_registration():
    reg = UMIPRegistry()

    @reg.participant("decorated", "local", S.REVERSIBLE, description="via decorator")
    def tool():
        return "ok"

    assert "decorated" in reg
    assert reg.get("decorated").description == "via decorator"


def test_default_registry_is_settable():
    original = get_registry()
    try:
        custom = UMIPRegistry()
        set_registry(custom)
        assert get_registry() is custom
    finally:
        set_registry(original)
