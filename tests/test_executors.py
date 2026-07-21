"""Thread-pool isolation and instrumentation.

The bug these exist to prevent: the WAL flusher sharing asyncio's default
executor with arbitrary blocking tool calls, so a burst of slow connectors
starves fsync and stalls every durable saga in the process.
"""

import asyncio
import threading
import time

import pytest

from agent_saga import ActionSemantics, AsyncWAL, Compensation, SagaContext
from agent_saga.executors import (
    BoundedExecutor,
    configure_tool_executor,
    get_tool_executor,
    new_wal_executor,
    set_tool_executor,
    tool_executor_stats,
)
from agent_saga.observability import current_correlation, set_saga_id
from conftest import aio

C = ActionSemantics.COMPENSABLE
R = ActionSemantics.REVERSIBLE


# --------------------------------------------------------------------------
# The isolation property -- the reason this module exists
# --------------------------------------------------------------------------

@aio
async def test_wal_fsync_is_not_starved_by_a_saturated_default_executor():
    """Regression test for head-of-line blocking.

    The engine used to flush the WAL with `asyncio.to_thread`, i.e. on the
    loop's *default* executor -- the same pool arbitrary blocking tool calls use.
    Saturate that pool and, under the old code, the flusher could not get a
    thread, so `barrier()` blocked and every durable saga in the process stalled
    behind unrelated slow connectors.

    This shrinks the default executor to two workers and occupies both, which is
    exactly that condition. It fails (times out) against the old shared-pool
    implementation and passes against the private flusher pool.
    """
    import tempfile
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path

    loop = asyncio.get_running_loop()
    starved = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-default")
    loop.set_default_executor(starved)
    release = threading.Event()
    entered = threading.Semaphore(0)

    async def hog():
        def _block():
            entered.release()      # we now hold one of the two threads
            release.wait(5)
        await asyncio.to_thread(_block)

    try:
        with tempfile.TemporaryDirectory() as d:
            wal = AsyncWAL(Path(d) / "wal.jsonl")
            await wal.start()

            hogs = [asyncio.create_task(hog()) for _ in range(2)]
            # Poll rather than wait on a thread: every default-executor thread is
            # about to be occupied, so anything needing one would deadlock here.
            for _ in range(2):
                while not entered.acquire(blocking=False):
                    await asyncio.sleep(0.01)

            # Both default threads are now blocked. The WAL must still reach
            # durability, because it owns a thread nothing else can take.
            wal.append("STEP_INTENT", {"saga_id": "s1"})
            await asyncio.wait_for(wal.barrier(), timeout=5.0)

            release.set()
            await asyncio.gather(*hogs)
            await wal.close()
    finally:
        release.set()
        starved.shutdown(wait=False)


@aio
async def test_wal_has_its_own_pool_distinct_from_the_tool_pool():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        wal = AsyncWAL(Path(d) / "wal.jsonl")
        await wal.start()
        assert wal._flush_pool is not None
        assert wal._flush_pool is not get_tool_executor()._pool
        await wal.close()
        assert wal._flush_pool is None      # joined, not leaked


# --------------------------------------------------------------------------
# Contextvar propagation -- correlation ids must survive the thread hop
# --------------------------------------------------------------------------

@aio
async def test_correlation_id_survives_the_thread_hop():
    """run_in_executor does not copy context; to_thread does. If we regressed to
    a naive run_in_executor, saga_id would vanish from every log emitted inside
    a sync connector."""
    seen = {}
    tok = set_saga_id("saga-thread-test")
    try:
        def blocking_tool():
            seen["saga_id"] = current_correlation()[0]
            seen["thread"] = threading.current_thread().name
        await get_tool_executor().run(blocking_tool)
    finally:
        from agent_saga.observability import reset_saga_id
        reset_saga_id(tok)

    assert seen["saga_id"] == "saga-thread-test"
    assert seen["thread"] != threading.main_thread().name   # really off-loop


# --------------------------------------------------------------------------
# Instrumentation
# --------------------------------------------------------------------------

@aio
async def test_saturation_is_counted_not_hidden():
    ex = BoundedExecutor(max_workers=1, name="test-sat")
    release = threading.Event()
    try:
        tasks = [asyncio.create_task(ex.run(lambda: release.wait(5)))
                 for _ in range(4)]
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.gather(*tasks)

        snap = ex.snapshot()
        assert snap["submitted"] == 4 and snap["completed"] == 4
        assert snap["saturated"] >= 3          # 3 arrived with the worker busy
        assert snap["peak_in_flight"] == 1     # bounded, as configured
        assert snap["max_queue_wait_ms"] > 0   # queueing was measured
    finally:
        release.set()
        ex.shutdown(wait=False)


@aio
async def test_stats_snapshot_is_exposed_for_scraping():
    await get_tool_executor().run(lambda: None)
    snap = tool_executor_stats()
    for key in ("max_workers", "submitted", "completed", "saturated",
                "avg_queue_wait_ms", "utilization"):
        assert key in snap


def test_configure_tool_executor_resizes():
    try:
        ex = configure_tool_executor(max_workers=7)
        assert ex.max_workers == 7
        assert get_tool_executor() is ex
    finally:
        set_tool_executor(None)


def test_rejects_a_nonsensical_pool_size():
    with pytest.raises(ValueError):
        BoundedExecutor(max_workers=0)


# --------------------------------------------------------------------------
# Concurrency: many sagas rolling back at once
# --------------------------------------------------------------------------

@aio
async def test_many_concurrent_sagas_roll_back_without_deadlock():
    """A rollback storm wider than the tool pool must complete, not deadlock:
    compensations queue on the bounded pool while the WAL keeps its own thread."""
    import tempfile
    from pathlib import Path

    small = BoundedExecutor(max_workers=4, name="test-storm")
    set_tool_executor(small)
    undone = []
    lock = threading.Lock()

    try:
        with tempfile.TemporaryDirectory() as d:
            async def one(i):
                wal = AsyncWAL(Path(d) / f"wal{i}.jsonl")
                await wal.start()
                ctx = SagaContext(wal=wal)
                await ctx.execute(
                    tool=f"tool{i}", semantics=C,
                    forward=lambda: {"id": i},
                    # A *sync* compensation: it must go through the bounded pool.
                    compensate=lambda r: Compensation(
                        fn=lambda: (time.sleep(0.005),
                                    lock.acquire(), undone.append(r["id"]),
                                    lock.release()),
                        handler="h"))
                report = await ctx.rollback()
                await wal.close()
                return report.clean

            results = await asyncio.wait_for(
                asyncio.gather(*[one(i) for i in range(40)]), timeout=30.0)

        assert all(results)
        assert len(undone) == 40
        assert small.stats.peak_in_flight <= 4   # the bound actually held
    finally:
        small.shutdown(wait=False)
        set_tool_executor(None)
