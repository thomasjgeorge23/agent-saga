"""Crash recovery: orphan detection, leases, and fail-closed escalation."""

import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from agent_saga import (
    ActionSemantics,
    RecoveryDaemon,
    Resolution,
    compensator,
    parse_wal,
    recovery_token,
)
from conftest import aio

# The daemon can only compensate handlers it has imported. Importing the worker
# module here is the test-suite equivalent of deploying saga-recoveryd with the
# same connector packages as the agent -- and the first failing run of this file
# demonstrated exactly what happens when you forget: NEEDS_HUMAN, not a crash.
import crash_worker  # noqa: F401  (registers "test.refund")

WORKER = Path(__file__).parent / "crash_worker.py"


# --------------------------------------------------------------------------
# Handlers used by the synthetic-WAL tests
# --------------------------------------------------------------------------

CALLS: list[dict] = []


@compensator("test.revert_crm")
def revert_crm(record_id):
    CALLS.append({"handler": "test.revert_crm", "record_id": record_id})


@compensator("test.explodes")
def explodes(**_):
    raise RuntimeError("Salesforce 503")


def _wal(tmp: Path, records: list[dict]) -> Path:
    path = tmp / "wal.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for i, r in enumerate(records, start=1):
            fh.write(json.dumps({"seq": i, **r}) + "\n")
    return path


def _saga_records(*, saga_id="s1", ts=None, semantics="COMPENSABLE",
                  handler="test.revert_crm", recoverable=True, committed=True,
                  terminal=None, lease_ttl=5.0):
    """A saga that started, ran one step, and then (by default) went silent."""
    ts = ts if ts is not None else time.time()
    recs = [{"event": "SAGA_START", "saga_id": saga_id, "ts": ts,
             "pid": 4242, "lease_ttl": lease_ttl},
            {"event": "STEP_INTENT", "saga_id": saga_id, "ts": ts, "step_id": "st1",
             "tool": "crm.update", "semantics": semantics, "kwargs": {}}]
    if committed:
        recs.append({"event": "STEP_COMMITTED", "saga_id": saga_id, "ts": ts,
                     "step_id": "st1", "tool": "crm.update", "semantics": semantics,
                     "compensation": {"handler": handler, "recoverable": recoverable,
                                      "kwargs": {"record_id": "acct_9"},
                                      "idempotency_key": "idem-1",
                                      "fn": "revert_crm", "description": ""}})
    if terminal:
        recs.append({"event": terminal, "saga_id": saga_id, "ts": ts, "clean": True})
        if terminal == "SAGA_ABORTED":
            recs.append({"event": "ROLLBACK_END", "saga_id": saga_id, "ts": ts, "clean": True})
    return recs


OLD = time.time() - 3600  # a lease from an hour ago is unambiguously dead


# --------------------------------------------------------------------------
# 1. WAL folding
# --------------------------------------------------------------------------

def test_parse_wal_folds_events_into_saga_state():
    sagas = parse_wal(_saga_records(ts=OLD))
    saga = sagas["s1"]
    assert saga.pid == 4242
    assert len(saga.steps) == 1
    assert saga.steps[0].state == "COMMITTED"
    assert saga.steps[0].tool == "crm.update"
    assert not saga.resolved_in_process
    assert saga.lease_expired()


def test_completed_saga_is_not_dangling():
    with tempfile.TemporaryDirectory() as d:
        path = _wal(Path(d), _saga_records(ts=OLD, terminal="SAGA_COMPLETE"))
        assert RecoveryDaemon(path).dangling() == []


def test_aborted_saga_that_finished_rollback_is_not_dangling():
    """The process already cleaned up. Touching it again would double-compensate."""
    with tempfile.TemporaryDirectory() as d:
        path = _wal(Path(d), _saga_records(ts=OLD, terminal="SAGA_ABORTED"))
        assert RecoveryDaemon(path).dangling() == []


def test_already_compensated_steps_are_not_retried():
    with tempfile.TemporaryDirectory() as d:
        recs = _saga_records(ts=OLD)
        recs.append({"event": "COMPENSATED", "saga_id": "s1", "ts": OLD,
                     "step_id": "st1", "tool": "crm.update"})
        path = _wal(Path(d), recs)
        assert RecoveryDaemon(path).dangling() == []


# --------------------------------------------------------------------------
# 2. Leases -- a live process must never be recovered out from under
# --------------------------------------------------------------------------

@aio
async def test_saga_with_a_live_lease_is_left_alone():
    CALLS.clear()
    with tempfile.TemporaryDirectory() as d:
        path = _wal(Path(d), _saga_records(ts=time.time()))
        daemon = RecoveryDaemon(path)
        saga = daemon.scan()["s1"]
        outcome = await daemon.recover(saga)
        assert outcome.resolution is Resolution.SKIPPED_ACTIVE
        assert CALLS == []


def test_lease_expiry_allows_two_ttls_of_grace():
    """A GC pause must not be mistaken for a dead process. False positives here
    cause double-compensation, which is worse than slow recovery."""
    saga = parse_wal(_saga_records(ts=1000.0, lease_ttl=5.0))["s1"]
    assert not saga.lease_expired(now=1009.0)   # inside 2x TTL
    assert saga.lease_expired(now=1011.0)


# --------------------------------------------------------------------------
# 3. The happy path
# --------------------------------------------------------------------------

@aio
async def test_dangling_saga_is_compensated_by_the_daemon():
    CALLS.clear()
    with tempfile.TemporaryDirectory() as d:
        path = _wal(Path(d), _saga_records(ts=OLD))
        outcomes = await RecoveryDaemon(path).recover_all()
        assert [o.resolution for o in outcomes] == [Resolution.RECOVERED]
        assert CALLS == [{"handler": "test.revert_crm", "record_id": "acct_9"}]


@aio
async def test_recovery_is_idempotent_across_daemon_restarts():
    """Deterministic tokens plus a journal: the second sweep sees the first
    sweep's success and declines. Double-refunds are structurally impossible."""
    CALLS.clear()
    with tempfile.TemporaryDirectory() as d:
        path = _wal(Path(d), _saga_records(ts=OLD))
        await RecoveryDaemon(path, daemon_id="d1").recover_all()
        await RecoveryDaemon(path, daemon_id="d2").recover_all()
        assert len(CALLS) == 1


@aio
async def test_two_daemons_racing_the_same_saga_only_one_acts():
    CALLS.clear()
    with tempfile.TemporaryDirectory() as d:
        path = _wal(Path(d), _saga_records(ts=OLD))
        d1 = RecoveryDaemon(path, daemon_id="d1")
        d2 = RecoveryDaemon(path, daemon_id="d2")
        saga = d1.scan()["s1"]

        # Hold the claim so the race is deterministic rather than timing-dependent.
        assert d1._claim("s1") is True
        outcome = await d2.recover(saga)
        assert outcome.resolution is Resolution.SKIPPED_CLAIMED
        assert CALLS == []


def test_recovery_token_is_deterministic_across_processes():
    a = recovery_token("saga-1", "step-1")
    b = recovery_token("saga-1", "step-1")
    assert a == b and len(a) == 32
    assert recovery_token("saga-1", "step-2") != a


# --------------------------------------------------------------------------
# 4. Fail closed -- the property a regulated buyer is actually purchasing
# --------------------------------------------------------------------------

@aio
async def test_irreversible_step_halts_recovery_and_escalates():
    """Case C. The daemon must not improvise around a sent wire or a sent email."""
    CALLS.clear()
    with tempfile.TemporaryDirectory() as d:
        path = _wal(Path(d), _saga_records(ts=OLD, semantics="IRREVERSIBLE"))
        outcome = (await RecoveryDaemon(path).recover_all())[0]
        assert outcome.resolution is Resolution.NEEDS_HUMAN
        assert outcome.escalated == ["crm.update"]
        assert CALLS == []


@aio
async def test_closure_compensation_escalates_instead_of_guessing():
    CALLS.clear()
    with tempfile.TemporaryDirectory() as d:
        path = _wal(Path(d), _saga_records(ts=OLD, recoverable=False))
        outcome = (await RecoveryDaemon(path).recover_all())[0]
        assert outcome.resolution is Resolution.NEEDS_HUMAN
        assert "in-process only" in outcome.reason
        assert CALLS == []


@aio
async def test_unregistered_handler_escalates_with_an_actionable_reason():
    """The daemon must import the same connectors as the agent. When it does
    not, say exactly that -- this is the most likely deployment mistake."""
    with tempfile.TemporaryDirectory() as d:
        path = _wal(Path(d), _saga_records(ts=OLD, handler="stripe.refund.v2"))
        outcome = (await RecoveryDaemon(path).recover_all())[0]
        assert outcome.resolution is Resolution.NEEDS_HUMAN
        assert "stripe.refund.v2" in outcome.reason
        assert "not registered" in outcome.reason


@aio
async def test_crash_before_commit_escalates_rather_than_assuming_it_never_ran():
    """STEP_INTENT with no terminal record: we cannot know whether the effect
    landed, and we have no descriptor to undo it with. Escalate."""
    with tempfile.TemporaryDirectory() as d:
        path = _wal(Path(d), _saga_records(ts=OLD, committed=False))
        outcome = (await RecoveryDaemon(path).recover_all())[0]
        assert outcome.resolution is Resolution.NEEDS_HUMAN
        assert "no compensation was recorded" in outcome.reason


@aio
async def test_failed_compensation_halts_and_journals():
    with tempfile.TemporaryDirectory() as d:
        path = _wal(Path(d), _saga_records(ts=OLD, handler="test.explodes"))
        daemon = RecoveryDaemon(path)
        outcome = (await daemon.recover_all())[0]
        assert outcome.resolution is Resolution.NEEDS_HUMAN
        assert "Salesforce 503" in outcome.reason

        events = [json.loads(l)["event"]
                  for l in daemon.journal_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert events == ["RECOVERY_ATTEMPT", "RECOVERY_FAILED"]


@aio
async def test_claim_is_released_even_when_recovery_fails():
    with tempfile.TemporaryDirectory() as d:
        path = _wal(Path(d), _saga_records(ts=OLD, handler="test.explodes"))
        daemon = RecoveryDaemon(path)
        await daemon.recover_all()
        assert not (daemon.claims_dir / "s1.claim").exists()


# --------------------------------------------------------------------------
# 5. Dry run -- how this actually gets adopted
# --------------------------------------------------------------------------

@aio
async def test_dry_run_narrates_without_touching_anything():
    CALLS.clear()
    with tempfile.TemporaryDirectory() as d:
        path = _wal(Path(d), _saga_records(ts=OLD))
        daemon = RecoveryDaemon(path, dry_run=True)
        outcome = (await daemon.recover_all())[0]
        assert outcome.resolution is Resolution.RECOVERED
        assert CALLS == []
        journal = daemon.journal_path.read_text(encoding="utf-8")
        assert "RECOVERY_DRY_RUN" in journal
        assert "acct_9" in journal


# --------------------------------------------------------------------------
# 6. End to end: a real process, really killed
# --------------------------------------------------------------------------

def _crash(mode: str, d: Path):
    wal = d / "wal.jsonl"
    effects = d / "effects.txt"
    proc = subprocess.run([sys.executable, str(WORKER), str(wal), str(effects), mode],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 9, f"worker did not crash as expected: {proc.stderr}"
    return wal, effects


@aio
async def test_real_crashed_process_is_recovered_end_to_end():
    """os._exit() mid-saga: no finally, no atexit, no rollback. The effect is
    on disk and nothing in-process will ever undo it."""
    with tempfile.TemporaryDirectory() as d:
        wal, effects = _crash("commit", Path(d))
        assert effects.read_text(encoding="utf-8").strip() == "charged:ch_crash_1"

        time.sleep(0.8)  # let the 0.3s lease expire (2x TTL grace)
        outcome = (await RecoveryDaemon(wal).recover_all())[0]

        assert outcome.resolution is Resolution.RECOVERED
        assert effects.read_text(encoding="utf-8").splitlines() == [
            "charged:ch_crash_1", "refunded:ch_crash_1"]


@aio
async def test_real_crashed_process_with_irreversible_step_escalates():
    with tempfile.TemporaryDirectory() as d:
        wal, effects = _crash("irreversible", Path(d))
        time.sleep(0.8)
        outcome = (await RecoveryDaemon(wal).recover_all())[0]
        assert outcome.resolution is Resolution.NEEDS_HUMAN
        assert effects.read_text(encoding="utf-8").strip() == "charged:ch_crash_1"


@aio
async def test_real_crashed_process_with_closure_compensation_escalates():
    with tempfile.TemporaryDirectory() as d:
        wal, _ = _crash("closure", Path(d))
        time.sleep(0.8)
        outcome = (await RecoveryDaemon(wal).recover_all())[0]
        assert outcome.resolution is Resolution.NEEDS_HUMAN
        assert "in-process only" in outcome.reason
