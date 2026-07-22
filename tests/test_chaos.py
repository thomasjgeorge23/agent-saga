"""Chaos: kill the process and check what the outside world looks like.

Every other test in this suite runs in one cooperative process. These do not.
A worker performs real, durable effects against a file-backed ledger and is then
killed with os._exit -- no atexit, no finally, no event-loop shutdown. Whatever
holds afterwards holds because the design is right, not because anything got to
clean up.

The claim under test is exactly-once compensation. The ledger records refund
*attempts* separately from refunds *applied*, so these tests can assert the
strong version -- that a second refund was never issued -- rather than the weak
one, that a payment processor's idempotency key absorbed it.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from conftest import aio

import chaos_worker  # noqa: F401  -- registers the "chaos.refund" handler
from chaos_ledger import Ledger

from agent_saga.integrity import verify
from agent_saga.recovery import RecoveryDaemon, Resolution
from agent_saga.wal import FileWAL

WORKER = Path(__file__).parent / "chaos_worker.py"

WORKER_LEASE_TTL = 0.3
LEASE_WAIT = WORKER_LEASE_TTL * 2 + 0.6
"""The daemon waits 2x the TTL before claiming a saga, so a GC pause or a
stalled disk is never mistaken for a dead process. Tests must outwait that or
they measure the daemon declining to touch a saga it thinks is alive -- which
is correct behaviour and a useless test."""


def crash(tmp_path, mode, steps=1):
    """Run a worker to its chosen death. Returns (wal_path, ledger)."""
    wal = tmp_path / "w.jsonl"
    ledger_path = tmp_path / "ledger.jsonl"
    proc = subprocess.run(
        [sys.executable, str(WORKER), str(wal), str(ledger_path), mode, str(steps)],
        capture_output=True, text=True, timeout=60)
    if mode == "clean":
        assert proc.returncode == 0, proc.stderr
    else:
        assert proc.returncode != 0, "the worker was supposed to die"
    return wal, Ledger(ledger_path)


async def recover(wal_path, tmp_path, *, runs=1):
    """Run the recovery daemon, optionally more than once."""
    import time

    time.sleep(LEASE_WAIT)          # let the dead worker's lease expire
    outcomes = []
    for _ in range(runs):
        daemon = RecoveryDaemon(str(wal_path), claims_dir=str(tmp_path / "claims"),
                                journal_path=str(tmp_path / "journal.jsonl"))
        for saga in await daemon.dangling_async():
            outcomes.append(await daemon.recover(saga))
    return outcomes


# ---------------------------------------------------------------------------
# 1. The control
# ---------------------------------------------------------------------------

@aio
async def test_a_clean_run_leaves_the_charge_standing(tmp_path):
    wal, ledger = crash(tmp_path, "clean", steps=2)
    assert len(ledger.charges()) == 2
    assert ledger.refund_attempts() == []
    assert ledger.outstanding() == 300

    outcomes = await recover(wal, tmp_path)
    assert all(o.resolution is Resolution.NOTHING_TO_DO for o in outcomes) or not outcomes
    assert ledger.refund_attempts() == [], "a completed saga must not be compensated"


# ---------------------------------------------------------------------------
# 2. Crash after the effect, before its compensation is durable
# ---------------------------------------------------------------------------

@aio
async def test_a_charge_orphaned_by_a_crash_is_recovered(tmp_path):
    """The dangerous window: the money moved and the process died before the
    descriptor that says how to undo it reached disk."""
    wal, ledger = crash(tmp_path, "after_effect")
    assert len(ledger.charges()) == 1, "the charge really happened"
    assert ledger.outstanding() == 100

    await recover(wal, tmp_path)

    # Whatever the daemon decides, it must not leave the customer charged
    # silently: either it compensated, or it escalated to a human. The one
    # unacceptable outcome is a clean report with money still out.
    resolved = ledger.outstanding() == 0
    escalated = any(r.get("event") == "RECOVERY_ESCALATED"
                    for r in _journal(tmp_path))
    assert resolved or escalated, (
        f"charge left outstanding with no escalation: {ledger.summary()}")


def _journal(tmp_path):
    import json

    path = tmp_path / "journal.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ---------------------------------------------------------------------------
# 3. Crash after the compensation descriptor is durable
# ---------------------------------------------------------------------------

@aio
async def test_a_crash_after_commit_is_compensated_exactly_once(tmp_path):
    wal, ledger = crash(tmp_path, "after_commit", steps=2)
    assert len(ledger.charges()) == 1
    assert ledger.outstanding() == 100

    await recover(wal, tmp_path)

    assert ledger.outstanding() == 0, f"not compensated: {ledger.summary()}"
    assert len(ledger.refund_attempts("ch_0")) == 1, (
        f"refund was issued more than once: {ledger.summary()}")


@aio
async def test_running_the_daemon_repeatedly_does_not_refund_twice(tmp_path):
    """The strong claim: the second refund is never *issued*, not merely
    absorbed by the processor's idempotency key."""
    wal, ledger = crash(tmp_path, "after_commit")
    await recover(wal, tmp_path, runs=4)

    attempts = ledger.refund_attempts("ch_0")
    assert len(attempts) == 1, (
        f"{len(attempts)} refund attempts after 4 daemon runs -- exactly-once "
        f"is being provided by the ledger, not by agent-saga")
    assert ledger.outstanding() == 0


@aio
async def test_two_daemons_racing_compensate_once(tmp_path):
    """Two nodes both notice the same orphan. Deterministic recovery tokens
    plus the journal must make double-compensation structurally impossible."""
    import asyncio
    import time

    wal, ledger = crash(tmp_path, "after_commit")
    time.sleep(LEASE_WAIT)

    async def run_daemon(name):
        daemon = RecoveryDaemon(str(wal), claims_dir=str(tmp_path / "claims"),
                                journal_path=str(tmp_path / "journal.jsonl"))
        return [await daemon.recover(s) for s in await daemon.dangling_async()]

    await asyncio.gather(run_daemon("a"), run_daemon("b"))

    assert len(ledger.refund_attempts("ch_0")) == 1, (
        f"racing daemons double-refunded: {ledger.summary()}")


# ---------------------------------------------------------------------------
# 4. Crash part-way through a rollback
# ---------------------------------------------------------------------------

@aio
async def test_a_rollback_interrupted_half_way_is_finished_not_repeated(tmp_path):
    wal, ledger = crash(tmp_path, "mid_compensation", steps=3)

    assert len(ledger.charges()) == 3
    applied_before = len(ledger.refunds_applied())
    assert applied_before == 1, "the worker should have undone exactly one step"

    await recover(wal, tmp_path)

    assert ledger.outstanding() == 0, (
        f"the interrupted rollback was not finished: {ledger.summary()}")
    for charge in ledger.charges():
        attempts = ledger.refund_attempts(charge["id"])
        assert len(attempts) == 1, (
            f"{charge['id']} was refunded {len(attempts)} times: {ledger.summary()}")


# ---------------------------------------------------------------------------
# 5. The log itself survives the crash
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["after_intent", "after_effect", "after_commit",
                                  "mid_compensation"])
@aio
async def test_the_hash_chain_survives_a_kill(tmp_path, mode):
    """A SIGKILL mid-write leaves a torn final line. The chain must verify over
    everything that did land -- an audit log that a crash invalidates is not an
    audit log, because crashes are exactly when it is read."""
    wal, _ = crash(tmp_path, mode, steps=3)
    records = FileWAL(wal).records()

    assert records, "nothing survived the crash"
    report = verify(records)
    assert report.intact, f"{mode}: {report.summary()}"


@aio
async def test_a_torn_final_line_does_not_hide_earlier_records(tmp_path):
    wal, _ = crash(tmp_path, "after_commit", steps=2)
    raw = wal.read_bytes()
    # Simulate the tear the OS might have left mid-fsync.
    wal.write_bytes(raw + b'{"event": "STEP_COMM')

    records = FileWAL(wal).records()
    assert records, "a torn tail swallowed the whole log"
    assert verify(records).intact


@aio
async def test_recovery_still_works_against_a_torn_log(tmp_path):
    wal, ledger = crash(tmp_path, "after_commit")
    wal.write_bytes(wal.read_bytes() + b'{"event": "STEP_')

    await recover(wal, tmp_path)
    assert ledger.outstanding() == 0
    assert len(ledger.refund_attempts("ch_0")) == 1


# ---------------------------------------------------------------------------
# 6. What a crash does NOT preserve -- stated, not hidden
# ---------------------------------------------------------------------------

@aio
async def test_a_crash_before_the_effect_leaves_nothing_to_undo(tmp_path):
    """The intent is durable and the charge never happened. The daemon may
    attempt an idempotent compensation; what it must never do is invent one."""
    wal, ledger = crash(tmp_path, "after_intent")
    assert ledger.charges() == [], "the charge should not have happened"

    await recover(wal, tmp_path)
    assert ledger.outstanding() == 0
    assert len(ledger.refunds_applied()) == 0, (
        "a refund was applied for a charge that never happened")


def test_in_process_budget_does_not_survive_a_crash(tmp_path):
    """Documented, because it is the one guarantee a crash genuinely breaks.

    Spend windows live in the limit store, not the WAL. With the in-process
    default, a crashed and restarted agent starts the window fresh -- so a
    crash-loop could spend the daily budget repeatedly. RedisLimitStore is not
    merely the multi-node answer; it is the crash-durable one.
    """
    from agent_saga.limits import InProcessLimitStore, LimitRequest

    store = InProcessLimitStore()
    store.reserve([LimitRequest("daily", "k", 900.0, 1000.0, 3600.0)])
    assert store.usage("k", 3600) == 900

    restarted = InProcessLimitStore()        # what a restart actually gets
    assert restarted.usage("k", 3600) == 0
