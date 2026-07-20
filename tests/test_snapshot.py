"""Snapshot-based REVERSIBLE steps.

The correctness bar is exactness: restore must return the target to *precisely*
its prior state, including undoing additions and re-adding deletions -- not a
shallow overwrite of the keys that happened to change.
"""

import asyncio

import pytest

from agent_saga import (
    ActionSemantics,
    AttributeSnapshot,
    AsyncWAL,
    MappingSnapshot,
    SagaContext,
    SequenceSnapshot,
    SetSnapshot,
    StepState,
    auto_strategy,
    reversible,
    saga,
)
from conftest import aio


async def _ctx(tmp=None):
    wal = AsyncWAL(tmp / "wal.jsonl" if tmp else None)
    await wal.start()
    return SagaContext(wal=wal), wal


# --------------------------------------------------------------------------
# Exact restore -- the property a shallow overwrite gets wrong
# --------------------------------------------------------------------------

@aio
async def test_added_key_is_removed_on_restore():
    """A mutation that adds a key must have that key gone after restore. A
    dict.update(snapshot) would leave it -- a partial undo."""
    d = {"a": 1}
    ctx, wal = await _ctx()
    await reversible(ctx, target=d, mutate=lambda x: x.update(b=2, c=3))
    assert d == {"a": 1, "b": 2, "c": 3}
    await ctx.rollback()
    await wal.close()
    assert d == {"a": 1}


@aio
async def test_deleted_key_is_reinstated_on_restore():
    d = {"a": 1, "b": 2}
    ctx, wal = await _ctx()
    await reversible(ctx, target=d, mutate=lambda x: x.pop("b"))
    assert d == {"a": 1}
    await ctx.rollback()
    await wal.close()
    assert d == {"a": 1, "b": 2}


@aio
async def test_changed_value_is_restored():
    d = {"status": "open"}
    ctx, wal = await _ctx()
    await reversible(ctx, target=d, mutate=lambda x: x.__setitem__("status", "won"))
    assert d == {"status": "won"}
    await ctx.rollback()
    await wal.close()
    assert d == {"status": "open"}


# --------------------------------------------------------------------------
# Deep-copy isolation -- the snapshot must be immune to later aliasing
# --------------------------------------------------------------------------

@aio
async def test_nested_mutation_does_not_leak_into_the_snapshot():
    """Snapshot by reference is the classic bug: mutate a nested list in place
    and the 'snapshot' mutates with it, so restore is a no-op."""
    d = {"items": [1, 2]}
    ctx, wal = await _ctx()

    def mutate(x):
        x["items"].append(3)   # in-place mutation of the nested list
        x["items"][0] = 99

    await reversible(ctx, target=d, mutate=mutate)
    assert d == {"items": [99, 2, 3]}
    await ctx.rollback()
    await wal.close()
    assert d == {"items": [1, 2]}


@aio
async def test_restore_is_isolated_from_post_restore_mutation():
    """After restore, mutating the target must not reach back into the stored
    snapshot (deepcopy on the way out, too)."""
    from agent_saga.snapshot import MappingSnapshot

    strat = MappingSnapshot()
    snap = strat.capture({"x": [1]})
    target = {}
    strat.restore(target, snap)
    target["x"].append(2)
    assert snap == {"x": [1]}  # unchanged


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------

@aio
async def test_list_target_restores_in_place():
    lst = [1, 2, 3]
    alias = lst  # an alias must see the restored contents
    ctx, wal = await _ctx()
    await reversible(ctx, target=lst, mutate=lambda x: x.extend([4, 5]))
    assert alias == [1, 2, 3, 4, 5]
    await ctx.rollback()
    await wal.close()
    assert alias == [1, 2, 3]


@aio
async def test_set_target_restores():
    s = {"a", "b"}
    ctx, wal = await _ctx()
    await reversible(ctx, target=s, mutate=lambda x: x.add("c"))
    assert s == {"a", "b", "c"}
    await ctx.rollback()
    await wal.close()
    assert s == {"a", "b"}


@aio
async def test_attribute_snapshot_restores_only_named_fields():
    class Model:
        def __init__(self):
            self.status = "open"
            self.rating = "warm"
            self.untouched = "keep"

    m = Model()
    ctx, wal = await _ctx()

    def mutate(obj):
        obj.status = "won"
        obj.rating = "hot"
        obj.untouched = "changed-by-someone-else"

    await reversible(ctx, target=m, mutate=mutate,
                     strategy=AttributeSnapshot(["status", "rating"]))
    await ctx.rollback()
    await wal.close()

    assert m.status == "open" and m.rating == "warm"
    # A field the snapshot did not cover is left as-is, not clobbered.
    assert m.untouched == "changed-by-someone-else"


def test_attribute_snapshot_rejects_missing_attribute():
    class Empty:
        pass

    with pytest.raises(AttributeError, match="missing attribute"):
        AttributeSnapshot(["nope"]).capture(Empty())


def test_attribute_snapshot_requires_at_least_one_attribute():
    with pytest.raises(ValueError):
        AttributeSnapshot([])


# --------------------------------------------------------------------------
# auto_strategy dispatch
# --------------------------------------------------------------------------

@pytest.mark.parametrize("target,expected", [
    ({}, "MappingSnapshot"),
    ([], "SequenceSnapshot"),
    (set(), "SetSnapshot"),
])
def test_auto_strategy_picks_by_shape(target, expected):
    assert type(auto_strategy(target)).__name__ == expected


@pytest.mark.parametrize("bad", ["a string", b"bytes", 42, 3.14, object()])
def test_auto_strategy_rejects_targets_with_no_in_place_mutation(bad):
    with pytest.raises(TypeError):
        auto_strategy(bad)


# --------------------------------------------------------------------------
# The reason this is REVERSIBLE and not COMPENSABLE
# --------------------------------------------------------------------------

@aio
async def test_reversible_snapshot_rides_the_fast_path_no_fsync():
    """In-process state does not survive a crash, so it needs no durable record.
    Skipping the barrier is a correctness argument, not just a speed one."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        ctx, wal = await _ctx(Path(td))
        await reversible(ctx, target={"a": 1}, mutate=lambda x: x.update(b=2))
        assert wal.barriers == 0
        assert ctx.stack[0].semantics is ActionSemantics.REVERSIBLE
        await ctx.rollback()
        await wal.close()


# --------------------------------------------------------------------------
# Robustness to UNKNOWN -- the differentiator from result-derived compensation
# --------------------------------------------------------------------------

@aio
async def test_restore_is_valid_even_when_the_mutation_raises_partway():
    """The snapshot predates the forward call, so a half-applied mutation is
    still fully reversible -- unlike a Stripe charge, whose inverse needs the
    id the failed call never returned."""
    d = {"a": 1, "b": 2}
    ctx, wal = await _ctx()

    def mutate(x):
        x["a"] = 99          # first half lands
        raise RuntimeError("boom")  # second half never runs

    with pytest.raises(RuntimeError):
        await reversible(ctx, target=d, mutate=mutate)

    assert d == {"a": 99, "b": 2}                 # partially mutated
    assert ctx.stack[0].state is StepState.UNKNOWN
    assert ctx.stack[0].compensation is not None   # restore still registered

    report = await ctx.rollback()
    await wal.close()
    assert report.clean
    assert d == {"a": 1, "b": 2}                    # fully restored anyway


# --------------------------------------------------------------------------
# LIFO across multiple snapshots + the @saga boundary
# --------------------------------------------------------------------------

@aio
async def test_multiple_snapshots_restore_last_in_first_out():
    d = {"n": 0}
    seen = []
    ctx, wal = await _ctx()

    for i in (1, 2, 3):
        await reversible(ctx, target=d,
                         mutate=lambda x, i=i: (x.__setitem__("n", i),
                                                seen.append(("do", i))),
                         tool=f"step{i}")

    # Wrap restore ordering observation by snapshotting values as we go.
    assert d == {"n": 3}
    await ctx.rollback()
    await wal.close()
    # After full LIFO rollback we are back at the very first snapshot.
    assert d == {"n": 0}


@aio
async def test_snapshot_inside_saga_boundary_auto_rolls_back():
    from agent_saga import current_saga

    state = {"balance": 100}
    log = {}

    @saga(reraise=False)
    async def run():
        await reversible(current_saga(), target=state,
                         mutate=lambda s: s.__setitem__("balance", 0))
        log["mid"] = dict(state)
        raise ValueError("model error")

    report = await run()
    assert log["mid"] == {"balance": 0}    # mutation applied inside the boundary
    assert state == {"balance": 100}       # rolled back by the boundary
    assert report.clean


# --------------------------------------------------------------------------
# Concurrency: two sagas snapshotting independent targets
# --------------------------------------------------------------------------

@aio
async def test_concurrent_snapshots_do_not_interfere():
    from agent_saga import current_saga

    a = {"v": "a0"}
    b = {"v": "b0"}

    @saga(reraise=False)
    async def fail_a():
        await reversible(current_saga(), target=a, mutate=lambda x: x.update(v="a1"))
        raise ValueError("boom")

    @saga(reraise=False)
    async def ok_b():
        await reversible(current_saga(), target=b, mutate=lambda x: x.update(v="b1"))

    await asyncio.gather(fail_a(), ok_b())
    assert a == {"v": "a0"}   # rolled back
    assert b == {"v": "b1"}   # committed, untouched by a's rollback
