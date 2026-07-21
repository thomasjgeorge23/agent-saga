"""Reads must not degrade as the log ages.

Every test here asserts on *how much work* is done, not just on the result: a
correct answer computed by scanning all history is exactly the failure mode
that turns a long-lived fleet into an outage.
"""

import tempfile
import time
from pathlib import Path

import pytest

from agent_saga.ledger import RedisLedger
from agent_saga.recovery import RecoveryDaemon, Resolution
from agent_saga.registry import compensator
from agent_saga.wal.redis_wal import RedisWAL
from conftest import aio

CALLS: list = []


@compensator("scale.undo")
def undo(**kw):
    CALLS.append(kw)


class CountingRedis:
    """Records every command so a test can assert on the access pattern."""

    def __init__(self):
        self.lists: dict = {}
        self.sets: dict = {}
        self.hashes: dict = {}
        self.counters: dict = {}
        self.calls: list = []

    async def rpush(self, key, *v):
        self.calls.append(("rpush", key))
        self.lists.setdefault(key, []).extend(v)
        return len(self.lists[key])

    async def lrange(self, key, start, end):
        self.calls.append(("lrange", key, start, end))
        items = self.lists.get(key, [])
        return items[start:] if end == -1 else items[start:end + 1]

    async def llen(self, key):
        return len(self.lists.get(key, []))

    async def ltrim(self, key, start, end):
        self.calls.append(("ltrim", key, start, end))
        items = self.lists.get(key, [])
        self.lists[key] = items[start:] if end == -1 else items[start:end + 1]
        return True

    async def incrby(self, key, n):
        self.counters[key] = self.counters.get(key, 0) + n
        return self.counters[key]

    async def sadd(self, key, member):
        self.calls.append(("sadd", key))
        self.sets.setdefault(key, set()).add(member)
        return 1

    async def smembers(self, key):
        self.calls.append(("smembers", key))
        return set(self.sets.get(key, set()))

    async def sismember(self, key, member):
        self.calls.append(("sismember", key))
        return member in self.sets.get(key, set())

    async def hincrby(self, key, field, n):
        self.hashes.setdefault(key, {})[field] = self.hashes.get(key, {}).get(field, 0) + n
        return self.hashes[key][field]

    async def hgetall(self, key):
        self.calls.append(("hgetall", key))
        return dict(self.hashes.get(key, {}))

    async def delete(self, key):
        self.lists.pop(key, None)
        self.counters.pop(key, None)
        return 1

    async def aclose(self):
        pass


def _saga_records(sid, ts, *, resolved):
    recs = [
        {"event": "SAGA_START", "saga_id": sid, "ts": ts, "pid": 1, "lease_ttl": 5.0},
        {"event": "STEP_COMMITTED", "saga_id": sid, "ts": ts, "step_id": "st1",
         "tool": "t", "semantics": "COMPENSABLE",
         "compensation": {"handler": "scale.undo", "recoverable": True,
                          "kwargs": {"sid": sid}, "idempotency_key": None,
                          "fn": "undo", "description": ""}},
    ]
    if resolved:
        recs.append({"event": "SAGA_COMPLETE", "saga_id": sid, "ts": ts, "clean": True})
    return recs


async def _seed(shared, sagas):
    wal = RedisWAL(client=shared, key="fleet")
    await wal.start()
    for sid, resolved in sagas:
        for r in _saga_records(sid, time.time() - 3600, resolved=resolved):
            wal.append(r.pop("event"), r)
    await wal.barrier()
    await wal.close()


# ==========================================================================
# Read amplification
# ==========================================================================

@aio
async def test_a_sweep_reads_the_log_once_not_once_per_saga():
    """recover() used to re-read the whole WAL for every saga, so a sweep with
    N dangling sagas did N+1 full reads -- quadratic in the log."""
    CALLS.clear()
    shared = CountingRedis()
    await _seed(shared, [(f"s{i}", False) for i in range(5)])

    wal = RedisWAL(client=shared, key="fleet")
    await wal.start()
    shared.calls.clear()
    with tempfile.TemporaryDirectory() as d:
        daemon = RecoveryDaemon(wal, journal_path=Path(d) / "j.jsonl",
                                claims_dir=Path(d) / "c")
        outcomes = await daemon.recover_all()
    await wal.close()

    assert len(outcomes) == 5
    assert all(o.resolution is Resolution.RECOVERED for o in outcomes)

    # One logical pass over the log, regardless of how many sagas it holds.
    scans = [c for c in shared.calls if c[0] == "lrange" and c[2] == 0]
    assert len(scans) == 1, f"expected 1 full read, got {len(scans)}"


@aio
async def test_read_all_pages_instead_of_materialising_everything_at_once():
    shared = CountingRedis()
    await _seed(shared, [(f"s{i}", True) for i in range(40)])   # 120 records

    wal = RedisWAL(client=shared, key="fleet")
    await wal.start()
    shared.calls.clear()
    records = await wal.read_all(chunk=25)
    await wal.close()

    assert len(records) == 120
    pages = [c for c in shared.calls if c[0] == "lrange"]
    assert len(pages) > 1, "must page, not fetch the whole list in one reply"
    assert all((c[3] - c[2] + 1) <= 25 for c in pages), "each page bounded by chunk"


# ==========================================================================
# Ledger: index lookups, not history scans
# ==========================================================================

@aio
async def test_completed_keys_reads_an_index_not_the_whole_journal():
    shared = CountingRedis()
    ledger = RedisLedger(client=shared, key="led", daemon_id="d1")
    for i in range(50):
        await ledger.record("RECOVERY_ATTEMPT", {"token": f"t{i}"})
        await ledger.record("RECOVERY_SUCCESS", {"token": f"t{i}"})

    shared.calls.clear()
    done = await ledger.completed_keys()
    assert len(done) == 50
    # The audit list is never scanned to answer this.
    assert not [c for c in shared.calls if c[0] == "lrange"]
    assert [c for c in shared.calls if c[0] == "smembers"]


@aio
async def test_attempts_reads_a_hash_not_the_whole_journal():
    shared = CountingRedis()
    ledger = RedisLedger(client=shared, key="led")
    for _ in range(3):
        await ledger.record("RECOVERY_ATTEMPT", {"token": "tok"})

    shared.calls.clear()
    assert (await ledger.attempts())["tok"] == 3
    assert not [c for c in shared.calls if c[0] == "lrange"]


@aio
async def test_is_completed_is_a_single_membership_check():
    shared = CountingRedis()
    ledger = RedisLedger(client=shared, key="led")
    await ledger.record("RECOVERY_SUCCESS", {"token": "abc"})
    assert await ledger.is_completed("abc") is True
    assert await ledger.is_completed("nope") is False


@aio
async def test_ledger_compaction_never_drops_the_completed_index():
    """Trimming the audit log is fine. Losing the completed-token set would
    re-open the double-compensation window it exists to close."""
    shared = CountingRedis()
    ledger = RedisLedger(client=shared, key="led")
    for i in range(30):
        await ledger.record("RECOVERY_SUCCESS", {"token": f"t{i}"})

    removed = await ledger.compact(keep_last=10)
    assert removed == 20
    assert len(shared.lists["led"]) == 10
    assert len(await ledger.completed_keys()) == 30      # index intact


# ==========================================================================
# WAL compaction
# ==========================================================================

@aio
async def test_compaction_drops_resolved_history_from_the_head():
    shared = CountingRedis()
    # Three resolved sagas, then one still running.
    await _seed(shared, [("done1", True), ("done2", True),
                         ("done3", True), ("live", False)])

    wal = RedisWAL(client=shared, key="fleet")
    await wal.start()
    before = len(await wal.read_all())
    removed = await wal.compact(keep_saga_ids={"live"})
    after = await wal.read_all()
    await wal.close()

    assert removed == 9                 # 3 resolved sagas x 3 records
    assert before == 11 and len(after) == 2
    assert {r["saga_id"] for r in after} == {"live"}


@aio
async def test_compaction_is_conservative_and_stops_at_the_first_live_saga():
    """A resolved saga behind a live one is kept. Never remove a record an
    unresolved saga might still need."""
    shared = CountingRedis()
    await _seed(shared, [("done1", True), ("live", False), ("done2", True)])

    wal = RedisWAL(client=shared, key="fleet")
    await wal.start()
    removed = await wal.compact(keep_saga_ids={"live"})
    remaining = {r["saga_id"] for r in await wal.read_all()}
    await wal.close()

    assert removed == 3                       # only the leading resolved run
    assert remaining == {"live", "done2"}     # done2 survives, behind live


@aio
async def test_compaction_is_a_no_op_when_the_head_is_still_live():
    shared = CountingRedis()
    await _seed(shared, [("live", False), ("done", True)])

    wal = RedisWAL(client=shared, key="fleet")
    await wal.start()
    assert await wal.compact(keep_saga_ids={"live"}) == 0
    assert len(await wal.read_all()) == 5
    await wal.close()


@aio
async def test_read_since_returns_only_what_arrived_after_the_cursor():
    shared = CountingRedis()
    await _seed(shared, [("a", True)])
    wal = RedisWAL(client=shared, key="fleet")
    await wal.start()
    first = await wal.read_all()
    cursor = max(r["gseq"] for r in first)

    wal.append("SAGA_START", {"saga_id": "b"})
    await wal.barrier()
    fresh = await wal.read_since(cursor)
    await wal.close()

    assert [r["saga_id"] for r in fresh] == ["b"]


# ==========================================================================
# FileWAL compaction -- the DEFAULT backend, which is what most people run
# ==========================================================================

@aio
async def test_file_wal_compaction_reclaims_resolved_sagas():
    """The file backend grew without bound: a node running for months keeps
    every saga it ever ran, and the daemon reads the whole file each sweep."""
    from agent_saga.wal import FileWAL

    with tempfile.TemporaryDirectory() as d:
        wal = FileWAL(Path(d) / "wal.jsonl")
        await wal.start()
        for sid in ("old1", "old2", "live"):
            wal.append("SAGA_START", {"saga_id": sid})
            wal.append("STEP_COMMITTED", {"saga_id": sid, "step_id": "s1"})
        await wal.barrier()

        before = len(await wal.read_all())
        removed = await wal.compact(keep_saga_ids={"live"})
        after = await wal.read_all()
        await wal.close()

    assert before == 6 and removed == 4
    assert {r["saga_id"] for r in after} == {"live"}


@aio
async def test_file_compaction_filters_rather_than_only_trimming_the_head():
    """Unlike a Redis list, a file can be rewritten -- so a resolved saga sitting
    BEHIND a live one is reclaimed too."""
    from agent_saga.wal import FileWAL

    with tempfile.TemporaryDirectory() as d:
        wal = FileWAL(Path(d) / "wal.jsonl")
        await wal.start()
        for sid in ("done1", "live", "done2"):
            wal.append("SAGA_START", {"saga_id": sid})
        await wal.barrier()
        await wal.compact(keep_saga_ids={"live"})
        remaining = {r["saga_id"] for r in await wal.read_all()}
        await wal.close()

    assert remaining == {"live"}          # done2 reclaimed despite being last


@aio
async def test_appends_during_compaction_are_not_lost():
    """append() only touches the in-memory buffer, so records written while the
    file is being swapped are flushed into the new file afterwards."""
    from agent_saga.wal import FileWAL

    with tempfile.TemporaryDirectory() as d:
        wal = FileWAL(Path(d) / "wal.jsonl")
        await wal.start()
        wal.append("SAGA_START", {"saga_id": "old"})
        await wal.barrier()

        wal.append("SAGA_START", {"saga_id": "live"})   # buffered, not yet on disk
        await wal.compact(keep_saga_ids={"live"})
        await wal.barrier()                            # flushes into the new file

        sagas = {r["saga_id"] for r in await wal.read_all()}
        await wal.close()

    assert sagas == {"live"}


@aio
async def test_compaction_leaves_no_temp_file_behind():
    from agent_saga.wal import FileWAL

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "wal.jsonl"
        wal = FileWAL(path)
        await wal.start()
        wal.append("SAGA_START", {"saga_id": "gone"})
        await wal.barrier()
        await wal.compact(keep_saga_ids=set())
        await wal.close()
        leftovers = [p.name for p in Path(d).iterdir() if p.name != "wal.jsonl"]

    assert leftovers == []


@aio
async def test_a_backend_without_compaction_raises_rather_than_no_opping():
    """A silent no-op would let an operator believe the log was bounded."""
    from agent_saga.wal.base import BufferedWAL, BackpressurePolicy

    class NoCompact(BufferedWAL):
        async def _flush_batch(self, batch): pass
        async def read_all(self): return []
        async def clear(self): pass

    wal = NoCompact()
    with pytest.raises(NotImplementedError, match="grow without bound"):
        await wal.compact(keep_saga_ids=set())


@aio
async def test_daemon_compaction_keeps_unresolved_and_recent_sagas():
    """The daemon computes the keep-set itself: unresolved sagas, sagas with
    stranded tentatives, and anything resolved inside the grace window."""
    from agent_saga.wal import FileWAL

    now = time.time()
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "wal.jsonl"
        wal = FileWAL(path)
        await wal.start()

        # Resolved long ago -> reclaimable. BOTH records must carry the old
        # timestamp: the keep-set uses a saga's most recent activity, so a fresh
        # SAGA_COMPLETE would correctly put it back inside the grace window.
        wal.append("SAGA_START", {"saga_id": "old", "ts": now - 99_999})
        wal.append("SAGA_COMPLETE", {"saga_id": "old", "clean": True,
                                     "ts": now - 99_999})
        # resolved just now -> inside the grace window, kept
        wal.append("SAGA_START", {"saga_id": "fresh"})
        wal.append("SAGA_COMPLETE", {"saga_id": "fresh", "clean": True})
        # never resolved -> always kept
        wal.append("SAGA_START", {"saga_id": "live"})
        await wal.barrier()

        daemon = RecoveryDaemon(wal, journal_path=Path(d) / "j.jsonl",
                                claims_dir=Path(d) / "c")
        removed = await daemon.compact(grace_seconds=3600)
        remaining = {r["saga_id"] for r in await wal.read_all()}
        await wal.close()

    assert removed == 2                             # the two 'old' records
    assert remaining == {"fresh", "live"}
