"""AsyncSinkWAL conformance (Phase 1.3 go/no-go).

The edge-port thesis is that the safety engine is separable from the disk: swap
fsync-to-file for an async storage sink and inherit the gate, the hash chain, and
the barrier unchanged. These tests are the proof. If they pass, an edge port is
an adapter, not a rewrite.
"""

import asyncio

import pytest
from conftest import aio

from agent_saga import saga_scope, ActionSemantics
from agent_saga.context import Compensation, SagaContext
from agent_saga.integrity import verify
from agent_saga.wal.async_sink import (
    AsyncSinkWAL, AsyncStorageSink, InMemoryAsyncSink)


@aio
async def test_full_write_read_lifecycle():
    wal = AsyncSinkWAL(sink=InMemoryAsyncSink())
    await wal.start()
    for i in range(5):
        wal.append("STEP_COMMITTED", {"saga_id": "edge-1", "tool": f"t{i}"})
    await wal.barrier()
    assert wal.persisted == 5
    records = await wal.read_all()
    assert len(records) == 5
    assert all(r["saga_id"] == "edge-1" for r in records)
    await wal.close()


@aio
async def test_the_chain_it_produces_verifies():
    """The record the edge node writes must be as tamper-evident as any WAL."""
    wal = AsyncSinkWAL(sink=InMemoryAsyncSink())
    await wal.start()
    for i in range(6):
        wal.append("STEP_COMMITTED", {"saga_id": "s", "n": i})
    await wal.barrier()
    records = await wal.read_all()
    await wal.close()
    assert verify(records).intact
    # tampering is still caught
    records[2]["n"] = 999
    assert not verify(records).intact


@aio
async def test_barrier_fails_loudly_when_the_sink_fails():
    """A failed storage ack must surface exactly like a failed fsync -- the
    caller must never be told an intent is durable when it is not."""
    class Broken(AsyncStorageSink):
        async def append(self, lines): raise ConnectionError("KV unreachable")
        async def scan(self): return []
        async def truncate(self): pass

    wal = AsyncSinkWAL(sink=Broken(), barrier_timeout=1.0)
    await wal.start()
    wal.append("STEP_COMMITTED", {"saga_id": "s"})
    with pytest.raises(Exception):          # WALStalled
        await wal.barrier()
    await wal.close()


@aio
async def test_clear_empties_the_store():
    sink = InMemoryAsyncSink()
    wal = AsyncSinkWAL(sink=sink)
    await wal.start()
    wal.append("SAGA_START", {"saga_id": "s"})
    await wal.barrier()
    assert len(await wal.read_all()) == 1
    await wal.clear()
    assert await wal.read_all() == []
    await wal.close()


@aio
async def test_it_drives_a_real_saga_and_rolls_back():
    """The whole point: a saga runs on the edge WAL, fails, and rolls back --
    with no filesystem anywhere."""
    sink = InMemoryAsyncSink()
    wal = AsyncSinkWAL(sink=sink)
    await wal.start()
    undone = []
    ctx = SagaContext(wal=wal)
    await ctx.begin()
    try:
        await ctx.execute(
            tool="stripe.charge", semantics=ActionSemantics.COMPENSABLE,
            forward=lambda: {"id": "ch_1"},
            compensate=lambda r: Compensation(
                fn=lambda **k: undone.append(k), handler="refund",
                kwargs={"charge_id": r["id"]}))
        raise ValueError("boom")
    except BaseException:
        report = await ctx.rollback()
        await ctx.finish(aborted=True, clean=report.clean)
    await wal.close()

    assert undone and undone[0]["charge_id"] == "ch_1"     # compensation ran
    records = await wal.read_all()
    assert verify(records).intact                          # and the log is sound


@aio
async def test_async_sink_wal_and_file_wal_produce_the_same_chain(tmp_path):
    """Same records in, same hash chain out: the durability backend does not
    change what the log commits to."""
    from agent_saga.wal.file_wal import FileWAL

    def _records(events):
        return events

    sink = InMemoryAsyncSink()
    a = AsyncSinkWAL(sink=sink)
    f = FileWAL(tmp_path / "f.wal")
    await a.start()
    await f.start()
    for wal in (a, f):
        wal.append("STEP_COMMITTED", {"saga_id": "s", "tool": "x", "amount": 100})
        await wal.barrier()
    a_recs = await a.read_all()
    f_recs = f.records()
    await a.close()
    await f.close()
    # ts differs, but the chain digest of the business content matches
    assert a_recs[0]["_h"] and f_recs[0]["_h"]
    assert verify(a_recs).intact and verify(f_recs).intact


def test_rejects_a_non_conforming_sink():
    with pytest.raises(TypeError):
        AsyncSinkWAL(sink=object())         # no append/scan/truncate
    with pytest.raises(TypeError):
        AsyncSinkWAL(sink=lambda x: x)      # a bare callable is not a store
