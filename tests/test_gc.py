"""Snapshot-store garbage collection.

The one thing the sweep must never do is delete a snapshot a rollback still
needs. Every test here is really a test of what it *keeps*.
"""

import json
import os
import tempfile
from pathlib import Path

from agent_saga import FileSnapshotStore, SnapshotGC
from conftest import aio

NOW = 1_000_000.0
OLD = NOW - 100_000        # well past the 1h grace
YOUNG = NOW - 10           # inside the grace


def _write_wal(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for i, r in enumerate(records, start=1):
            fh.write(json.dumps({"seq": i, **r}) + "\n")


def _durable_saga(sid: str, snap: str, ts: float, *, complete=False,
                  aborted=False, rolledback=False) -> list[dict]:
    recs = [
        {"event": "SAGA_START", "saga_id": sid, "ts": ts, "pid": 1},
        {"event": "STEP_INTENT", "saga_id": sid, "ts": ts, "step_id": "s1",
         "tool": "durable.file", "semantics": "COMPENSABLE"},
        {"event": "STEP_COMMITTED", "saga_id": sid, "ts": ts, "step_id": "s1",
         "tool": "durable.file", "semantics": "COMPENSABLE",
         "compensation": {"handler": "durable.restore_file", "recoverable": True,
                          "kwargs": {"snapshot_id": snap, "path": "/x",
                                     "existed": True, "guard_sha": "h"}}},
    ]
    if complete:
        recs.append({"event": "SAGA_COMPLETE", "saga_id": sid, "ts": ts, "clean": True})
    if aborted:
        recs.append({"event": "SAGA_ABORTED", "saga_id": sid, "ts": ts})
    if rolledback:
        recs.append({"event": "ROLLBACK_END", "saga_id": sid, "ts": ts, "clean": True})
    return recs


def _setup(records, snaps):
    d = tempfile.mkdtemp()
    tmp = Path(d)
    wal = tmp / "wal.jsonl"
    _write_wal(wal, records)
    store = FileSnapshotStore(tmp / "snaps")
    for s in snaps:
        store.put(s, b"snapshot-bytes")
    return tmp, wal, store


# --------------------------------------------------------------------------
# Delete only when safe
# --------------------------------------------------------------------------

def test_resolved_and_aged_out_saga_snapshot_is_deleted():
    _, wal, store = _setup(_durable_saga("s1", "snapA", OLD, complete=True), ["snapA"])
    report = SnapshotGC(wal, store, grace_seconds=3600).collect(now=NOW)
    assert report.deleted == ["snapA"]
    assert store.list_ids() == []


def test_rolled_back_saga_snapshot_is_deleted():
    _, wal, store = _setup(
        _durable_saga("s1", "snapA", OLD, aborted=True, rolledback=True), ["snapA"])
    report = SnapshotGC(wal, store, grace_seconds=3600).collect(now=NOW)
    assert report.deleted == ["snapA"]


# --------------------------------------------------------------------------
# Keep when a rollback might still need it
# --------------------------------------------------------------------------

def test_dangling_saga_snapshot_is_kept():
    """No terminal record -- the saga is running or crashed and unrecovered. Its
    undo data must survive."""
    _, wal, store = _setup(_durable_saga("s1", "snapA", OLD), ["snapA"])
    report = SnapshotGC(wal, store, grace_seconds=3600).collect(now=NOW)
    assert report.deleted == []
    assert report.kept_active == 1
    assert store.list_ids() == ["snapA"]


def test_resolved_but_young_saga_snapshot_is_kept():
    """Grace period: a saga that resolved a moment ago is left alone, so the
    sweep never races an in-flight rollback or daemon recovery."""
    _, wal, store = _setup(_durable_saga("s1", "snapA", YOUNG, complete=True), ["snapA"])
    report = SnapshotGC(wal, store, grace_seconds=3600).collect(now=NOW)
    assert report.deleted == []
    assert report.kept_young == 1


# --------------------------------------------------------------------------
# Recovery journal integration
# --------------------------------------------------------------------------

def test_daemon_recovered_saga_snapshot_is_deleted():
    """A crashed saga has no terminal WAL record; only the daemon journal knows
    it was compensated."""
    tmp, wal, store = _setup(_durable_saga("s1", "snapA", OLD), ["snapA"])
    journal = tmp / "recovery.jsonl"
    journal.write_text(json.dumps(
        {"event": "RECOVERY_SUCCESS", "saga_id": "s1", "ts": OLD}) + "\n")
    report = SnapshotGC(wal, store, recovery_journal=journal,
                        grace_seconds=3600).collect(now=NOW)
    assert report.deleted == ["snapA"]


def test_escalated_saga_snapshot_is_kept_for_the_human():
    tmp, wal, store = _setup(_durable_saga("s1", "snapA", OLD), ["snapA"])
    journal = tmp / "recovery.jsonl"
    journal.write_text(json.dumps(
        {"event": "RECOVERY_ESCALATED", "saga_id": "s1", "ts": OLD}) + "\n")
    report = SnapshotGC(wal, store, recovery_journal=journal,
                        grace_seconds=3600).collect(now=NOW)
    assert report.deleted == []
    assert report.kept_active == 1


# --------------------------------------------------------------------------
# Unreferenced orphans -- age-gated, since we cannot prove they are dead
# --------------------------------------------------------------------------

def test_old_unreferenced_orphan_is_reaped():
    tmp, wal, store = _setup(_durable_saga("s1", "snapA", OLD, complete=True), ["snapA"])
    # A blob referenced by no saga (e.g. a pre-commit crash): age it past grace.
    store.put("orphan", b"leftover")
    op = store.root / "orphan"
    old = os.stat(op).st_mtime - 100_000
    os.utime(op, (old, old))

    report = SnapshotGC(wal, store, grace_seconds=3600).collect(now=NOW)
    assert "orphan" in report.deleted


def test_young_unreferenced_orphan_is_kept():
    """A just-written blob whose STEP_COMMITTED has not landed yet must not be
    reaped out from under an in-flight saga."""
    tmp, wal, store = _setup([], [])
    store.put("fresh", b"just-written")
    report = SnapshotGC(wal, store, grace_seconds=3600).collect(now=NOW + 1)
    assert report.deleted == []
    assert report.kept_unreferenced == 1


# --------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------

def test_dry_run_reports_but_deletes_nothing():
    _, wal, store = _setup(_durable_saga("s1", "snapA", OLD, complete=True), ["snapA"])
    report = SnapshotGC(wal, store, grace_seconds=3600, dry_run=True).collect(now=NOW)
    assert report.deleted == ["snapA"]        # would delete
    assert store.list_ids() == ["snapA"]      # but did not


def test_gc_tolerates_a_truncated_wal():
    tmp = Path(tempfile.mkdtemp())
    wal = tmp / "wal.jsonl"
    recs = _durable_saga("s1", "snapA", OLD, complete=True)
    with open(wal, "w", encoding="utf-8") as fh:
        for i, r in enumerate(recs, start=1):
            fh.write(json.dumps({"seq": i, **r}) + "\n")
        fh.write('{"seq": 99, "event": "STEP_INT')   # torn line
    store = FileSnapshotStore(tmp / "snaps")
    store.put("snapA", b"x")
    report = SnapshotGC(wal, store, grace_seconds=3600).collect(now=NOW)
    assert report.deleted == ["snapA"]
