"""SagaFleet: cross-framework orchestration that can be audited.

The two silent failures this exists to remove, both measured rather than
assumed:

1. The saga contextvar does not cross a raw thread or a ThreadPoolExecutor.
   A framework that dispatches tools through its own executor makes
   `current_saga()` return None there, and the ordinary runner then performs
   the side effect with no WAL record and no compensation -- while every log
   line still says the tool is saga-aware.
2. An unwrapped or uncompensated tool is indistinguishable from a protected
   one until the day its effect needs undoing.
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import aio

from agent_saga import ActionSemantics, AsyncWAL, Compensation, SagaAborted
from agent_saga.decorator import current_saga
from agent_saga.fleet import BoundaryRequired, SagaFleet, bind_saga

C = ActionSemantics.COMPENSABLE
I = ActionSemantics.IRREVERSIBLE


def make_call(world, key, *, fail=False):
    async def call(**kwargs):
        if fail:
            raise ConnectionError(f"{key} unreachable")
        entry = f"{key}_{len(world[key]) + 1}"
        world[key].append(entry)
        return {"id": entry}
    return call


def undo_factory(world, key):
    def undo(entry_id):
        world[key] = [e for e in world[key] if e != entry_id]
    return lambda r: (Compensation(fn=undo, kwargs={"entry_id": r["id"]},
                                   description=f"undo {key}") if r else None)


# -- 1. the thread gap is real, and bind_saga closes it -------------------------------

@aio
async def test_the_contextvar_really_does_not_cross_a_thread(tmp_path):
    """Documents the gap this module exists for. If this ever starts passing,
    bind_saga is no longer needed -- and the docstring should stop claiming
    otherwise."""
    from agent_saga import saga_scope

    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        async with saga_scope(wal=wal):
            assert current_saga() is not None
            with ThreadPoolExecutor(1) as pool:
                assert pool.submit(lambda: current_saga() is None).result()
    finally:
        await wal.close()


@aio
async def test_bind_saga_carries_the_boundary_onto_a_worker_thread(tmp_path):
    from agent_saga import saga_scope

    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        async with saga_scope(wal=wal):
            with ThreadPoolExecutor(1) as pool:
                seen = pool.submit(bind_saga(lambda: current_saga() is not None)).result()
            assert seen, "bind_saga did not re-attach the saga"

            result = {}
            thread = threading.Thread(
                target=bind_saga(lambda: result.update(ok=current_saga() is not None)))
            thread.start()
            thread.join()
            assert result["ok"], "bind_saga did not survive a raw Thread"
    finally:
        await wal.close()


def test_bind_saga_is_harmless_with_no_saga():
    assert bind_saga(lambda: current_saga())() is None


@aio
async def test_a_tool_dispatched_to_a_thread_still_joins_the_fleets_saga(tmp_path):
    """The production case: a framework runs the tool on its own executor. The
    fleet re-attaches, so the effect is recorded and unwound."""
    world = {"crew": []}
    fleet = SagaFleet("threaded")
    tool = fleet.register(make_call(world, "crew"), name="crew.reserve",
                          framework="crewai", semantics=C,
                          compensate=undo_factory(world, "crew"))

    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        with pytest.raises(SagaAborted):
            async with fleet.transaction(wal=wal):
                loop = asyncio.get_running_loop()

                def framework_worker():
                    """A framework's own thread, with no help from us: the
                    contextvar is absent here, and nothing is bound. The
                    coroutine is scheduled back onto the saga's loop, because
                    the WAL's flush task lives there.

                    The fleet is what makes this work -- its runner falls back
                    to the transaction's active saga rather than taking the
                    outside-a-saga path and performing the effect unprotected.
                    """
                    assert current_saga() is None
                    future = asyncio.run_coroutine_threadsafe(
                        tool(amount=1), loop)
                    return future.result(timeout=30)

                with ThreadPoolExecutor(1) as pool:
                    await loop.run_in_executor(pool, framework_worker)

                assert world["crew"] == ["crew_1"]
                raise RuntimeError("a later step failed")
        records = await wal.read_all()
    finally:
        await wal.close()

    assert world["crew"] == [], "the threaded tool's effect was not rolled back"
    assert any(r.get("tool") == "crew.reserve" for r in records), \
        "the threaded tool never reached the WAL"


# -- 2. unprotected calls are refused, not silently performed ---------------------------

@aio
async def test_calling_outside_a_boundary_is_refused_by_default():
    world = {"crew": []}
    fleet = SagaFleet("strict")
    tool = fleet.register(make_call(world, "crew"), name="crew.reserve",
                          framework="crewai", semantics=C,
                          compensate=undo_factory(world, "crew"))

    with pytest.raises(BoundaryRequired, match="no active saga"):
        await tool(amount=1)
    assert world["crew"] == [], "the side effect happened anyway"


@aio
async def test_opting_out_is_possible_and_visible():
    world = {"read": []}
    fleet = SagaFleet("lenient")
    tool = fleet.register(make_call(world, "read"), name="search.query",
                          framework="langgraph", semantics=C,
                          compensate=undo_factory(world, "read"),
                          require_boundary=False)

    assert await tool() == {"id": "read_1"}          # allowed
    concerns = [t.name for t in fleet.coverage().concerns]
    assert "search.query" in concerns                # and flagged


# -- the boundary spans frameworks -------------------------------------------------------

@aio
async def test_one_fleet_transaction_spans_three_frameworks(tmp_path):
    world = {"crew": [], "lang": [], "auto": []}
    fleet = SagaFleet("content")
    reserve = fleet.register(make_call(world, "crew"), name="research.reserve",
                             framework="crewai", semantics=C,
                             compensate=undo_factory(world, "crew"))
    publish = fleet.register(make_call(world, "lang"), name="writer.publish",
                             framework="langgraph", semantics=C,
                             compensate=undo_factory(world, "lang"))
    # The failing tool's inverse must be valid on an UNKNOWN outcome: the call
    # raised, so there is no result to derive an id from, but the request may
    # still have landed. An inverse whose target was known beforehand covers
    # that; one derived from the result cannot, and the engine correctly
    # reports the step ORPHANED instead.
    notify = fleet.register(make_call(world, "auto", fail=True),
                            name="reviewer.notify", framework="autogen",
                            semantics=C,
                            compensate=lambda _r: Compensation(
                                fn=lambda: world["auto"].clear(),
                                description="retract the notification if it went out"))

    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        with pytest.raises(SagaAborted) as excinfo:
            async with fleet.transaction(wal=wal):
                await reserve(amount=1)
                await publish(title="Q3")
                await notify(channel="#x")
        records = await wal.read_all()
    finally:
        await wal.close()

    assert world["crew"] == [] and world["lang"] == []
    assert excinfo.value.report.clean
    assert [s.tool for s in excinfo.value.report.compensated] == [
        "reviewer.notify", "writer.publish", "research.reserve"]      # LIFO
    assert len({r["saga_id"] for r in records if r.get("saga_id")}) == 1


# -- coverage is the product ------------------------------------------------------------

def test_coverage_names_every_tool_and_its_framework():
    fleet = SagaFleet("audit")
    fleet.register(make_call({"a": []}, "a"), name="one.tool", framework="crewai",
                   semantics=C, compensate=lambda r: None)
    fleet.register(make_call({"b": []}, "b"), name="two.tool", framework="langgraph",
                   semantics=C, compensate=lambda r: None)

    report = fleet.coverage()
    assert report.frameworks == ("crewai", "langgraph")
    assert {t.name for t in report.tools} == {"one.tool", "two.tool"}
    assert report.fully_covered
    assert "fully covered: True" in report.format_text()


def test_a_missing_compensation_is_a_named_concern():
    fleet = SagaFleet("gap")
    fleet.register(make_call({"a": []}, "a"), name="risky.tool",
                   framework="autogen", semantics=C)      # no compensate

    report = fleet.coverage()
    assert not report.fully_covered
    concern = report.concerns[0]
    assert concern.name == "risky.tool"
    assert "ORPHANED" in concern.concern
    with pytest.raises(AssertionError, match="not fully covered"):
        fleet.assert_fully_covered()


def test_an_irreversible_tool_needs_no_inverse_to_be_covered():
    """The gate stops it before it runs, so there is nothing to undo."""
    fleet = SagaFleet("gated")
    fleet.register(make_call({"a": []}, "a"), name="email.send",
                   framework="crewai", semantics=I)
    assert fleet.coverage().fully_covered


def test_an_empty_fleet_is_not_reported_as_covered():
    """Vacuous success is the failure mode this whole package exists to
    avoid."""
    report = SagaFleet("empty").coverage()
    assert not report.fully_covered
    assert "protects nothing" in report.format_text()


def test_duplicate_registration_is_refused():
    fleet = SagaFleet("dupes")
    fleet.register(make_call({"a": []}, "a"), name="same", framework="crewai",
                   semantics=C, compensate=lambda r: None)
    with pytest.raises(ValueError, match="already registered"):
        fleet.register(make_call({"a": []}, "a"), name="same",
                       framework="langgraph", semantics=C,
                       compensate=lambda r: None)


@aio
async def test_nested_transactions_are_refused(tmp_path):
    """Nesting would leave the fleet's active saga pointing at whichever exited
    last, so a tool on a worker thread would join the wrong transaction."""
    fleet = SagaFleet("nested")
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        async with fleet.transaction(wal=wal):
            with pytest.raises(RuntimeError, match="already has an open transaction"):
                async with fleet.transaction(wal=wal):
                    pass
    finally:
        await wal.close()


@aio
async def test_the_active_saga_is_released_after_the_transaction(tmp_path):
    fleet = SagaFleet("release")
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        async with fleet.transaction(wal=wal):
            pass
        async with fleet.transaction(wal=wal):      # reusable, not stuck
            pass
    finally:
        await wal.close()
