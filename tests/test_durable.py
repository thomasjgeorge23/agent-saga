"""Durable-target snapshots: crash-recoverable file restore."""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from agent_saga import (
    ActionSemantics,
    AsyncWAL,
    FileSnapshotStore,
    RecoveryDaemon,
    Resolution,
    SagaContext,
    StaleFile,
    restore_file,
    set_snapshot_store,
    snapshot_file,
)
from conftest import aio


async def _ctx(tmp: Path):
    wal = AsyncWAL(tmp / "wal.jsonl")
    await wal.start()
    return SagaContext(wal=wal), wal


def _store(tmp: Path) -> FileSnapshotStore:
    store = FileSnapshotStore(tmp / "snapshots")
    set_snapshot_store(store)   # so the compensator handler resolves the same one
    return store


# --------------------------------------------------------------------------
# In-process rollback
# --------------------------------------------------------------------------

@aio
async def test_existing_file_is_restored_to_prior_contents():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _store(tmp)
        f = tmp / "config.yaml"
        f.write_text("mode: safe\n")

        ctx, wal = await _ctx(tmp)
        await snapshot_file(ctx, path=f,
                            mutate=lambda p: Path(p).write_text("mode: YOLO\n"))
        assert f.read_text() == "mode: YOLO\n"

        report = await ctx.rollback()
        await wal.close()
        assert report.clean
        assert f.read_text() == "mode: safe\n"


@aio
async def test_saga_created_file_is_deleted_on_rollback():
    """existed=False -> the undo is to remove the file the saga created."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _store(tmp)
        f = tmp / "generated.txt"

        ctx, wal = await _ctx(tmp)
        await snapshot_file(ctx, path=f,
                            mutate=lambda p: Path(p).write_text("agent output"))
        assert f.exists()

        report = await ctx.rollback()
        await wal.close()
        assert report.clean
        assert not f.exists()


@aio
async def test_binary_content_round_trips():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _store(tmp)
        f = tmp / "blob.bin"
        original = bytes(range(256)) * 8
        f.write_bytes(original)

        ctx, wal = await _ctx(tmp)
        await snapshot_file(ctx, path=f, mutate=lambda p: Path(p).write_bytes(b"\x00"))
        await ctx.rollback()
        await wal.close()
        assert f.read_bytes() == original


# --------------------------------------------------------------------------
# It is COMPENSABLE, and the WAL carries a reference, not the bytes
# --------------------------------------------------------------------------

@aio
async def test_durable_snapshot_is_compensable_and_barrier_backed():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _store(tmp)
        f = tmp / "f.txt"
        f.write_text("x")
        ctx, wal = await _ctx(tmp)
        await snapshot_file(ctx, path=f, mutate=lambda p: Path(p).write_text("y"))
        assert ctx.stack[0].semantics is ActionSemantics.COMPENSABLE
        assert wal.barriers >= 1     # unlike in-process reversible, this is durable
        await ctx.rollback()
        await wal.close()


@aio
async def test_file_bytes_never_enter_the_wal():
    """A large file's contents must not be fsynced into the log -- only a
    snapshot reference."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _store(tmp)
        f = tmp / "secretish.txt"
        marker = "UNIQUE_PRIOR_CONTENT_1234567890"
        f.write_text(marker)
        ctx, wal = await _ctx(tmp)
        await snapshot_file(ctx, path=f, mutate=lambda p: Path(p).write_text("new"))
        await ctx.rollback()
        await wal.close()

        raw = (tmp / "wal.jsonl").read_text()
        assert marker not in raw
        # the descriptor carries a snapshot id and is recoverable
        committed = [json.loads(l) for l in raw.splitlines()
                     if json.loads(l)["event"] == "STEP_COMMITTED"][0]
        assert committed["compensation"]["recoverable"] is True
        assert committed["compensation"]["kwargs"]["snapshot_id"]


# --------------------------------------------------------------------------
# The guard -- refuse to clobber a concurrent external edit
# --------------------------------------------------------------------------

def test_restore_refuses_when_the_file_changed_after_our_write():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        store = _store(tmp)
        f = tmp / "f.txt"
        store.put("snap1", b"original")
        f.write_text("edited by someone else")   # not what the saga wrote

        with pytest.raises(StaleFile, match="changed after"):
            restore_file(path=str(f), existed=True, snapshot_id="snap1",
                         guard_sha="deadbeef")    # guard won't match


@aio
async def test_guard_matches_our_own_write_and_allows_restore():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _store(tmp)
        f = tmp / "f.txt"
        f.write_text("before")
        ctx, wal = await _ctx(tmp)
        await snapshot_file(ctx, path=f, mutate=lambda p: Path(p).write_text("after"))
        # nobody else touched f, so the guard (sha of "after") matches
        report = await ctx.rollback()
        await wal.close()
        assert report.clean and f.read_text() == "before"


# --------------------------------------------------------------------------
# UNKNOWN outcome -- prior captured before the mutation, so restore still works
# --------------------------------------------------------------------------

@aio
async def test_failed_mutation_still_restores_prior_contents():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _store(tmp)
        f = tmp / "f.txt"
        f.write_text("safe")

        def bad_mutate(p):
            Path(p).write_text("half")   # partial write lands
            raise RuntimeError("mutation blew up")

        ctx, wal = await _ctx(tmp)
        with pytest.raises(RuntimeError):
            await snapshot_file(ctx, path=f, mutate=bad_mutate)
        report = await ctx.rollback()
        await wal.close()
        assert report.clean
        assert f.read_text() == "safe"


# --------------------------------------------------------------------------
# End to end: a real crashed process recovered by the daemon
# --------------------------------------------------------------------------

CRASH = '''
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, {root!r})
from agent_saga import AsyncWAL, SagaContext, FileSnapshotStore, set_snapshot_store, snapshot_file

async def main(wal_path, store_dir, target):
    set_snapshot_store(FileSnapshotStore(store_dir))
    wal = AsyncWAL(wal_path)
    await wal.start()
    ctx = SagaContext(wal=wal, lease_ttl=0.3)
    await ctx.begin()
    await snapshot_file(ctx, path=target,
                        mutate=lambda p: Path(p).write_text("MUTATED BY AGENT"))
    os._exit(9)   # die: no rollback, no SAGA_COMPLETE

asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3]))
'''


@aio
async def test_real_crash_is_recovered_by_the_daemon_end_to_end():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        root = str(Path(__file__).resolve().parent.parent)
        wal_path = tmp / "wal.jsonl"
        store_dir = tmp / "snaps"
        target = tmp / "config.txt"
        target.write_text("ORIGINAL")

        script = tmp / "crash.py"
        script.write_text(CRASH.format(root=root))
        proc = subprocess.run([sys.executable, str(script), str(wal_path),
                               str(store_dir), str(target)],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 9, proc.stderr
        assert target.read_text() == "MUTATED BY AGENT"   # effect is on disk

        # The daemon must resolve the same snapshot store the agent used.
        set_snapshot_store(FileSnapshotStore(store_dir))
        time.sleep(0.8)   # let the 0.3s lease expire (2x grace)

        # restore_file is registered by importing agent_saga.durable (done above).
        outcome = (await RecoveryDaemon(wal_path).recover_all())[0]
        assert outcome.resolution is Resolution.RECOVERED
        assert target.read_text() == "ORIGINAL"           # rolled back by the daemon
