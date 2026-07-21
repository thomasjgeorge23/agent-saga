"""Deterministic idempotency guardrails, and the backend-agnostic daemon.

The scenario that matters: a compensation succeeded, then the process died
before it could record that it had. On the next sweep the daemon must recognise
the completed work and NOT run it again -- because running a refund twice is
worse than never running it at all.
"""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from agent_saga import ActionSemantics, AsyncWAL, Compensation, SagaContext
from agent_saga.idempotency import IdempotencyManager
from agent_saga.recovery import RecoveryDaemon, Resolution, recovery_token
from agent_saga.registry import compensator
from conftest import aio

C = ActionSemantics.COMPENSABLE

CALLS: list[dict] = []


@compensator("idem.refund")
def refund(charge_id, idempotency_key=None):
    CALLS.append({"charge_id": charge_id, "idempotency_key": idempotency_key})
    return {"status": "refunded"}


@compensator("idem.no_kwargs")
def no_kwargs(charge_id):
    """A handler that cannot accept the key -- injection must not break it."""
    CALLS.append({"charge_id": charge_id})
    return {"status": "ok"}


# ==========================================================================
# Deterministic key derivation
# ==========================================================================

def test_key_is_stable_for_the_same_saga_and_step():
    a = IdempotencyManager.key("saga-1", "step-1")
    b = IdempotencyManager.key("saga-1", "step-1")
    assert a == b and len(a) == 32


def test_key_differs_per_step_and_per_saga():
    base = IdempotencyManager.key("saga-1", "step-1")
    assert IdempotencyManager.key("saga-1", "step-2") != base
    assert IdempotencyManager.key("saga-2", "step-1") != base


def test_scope_separates_distinct_operations_on_one_step():
    assert (IdempotencyManager.key("s", "st", scope="compensate")
            != IdempotencyManager.key("s", "st", scope="redrive"))


def test_key_survives_a_process_restart():
    """Derived only from stable identifiers, so a fresh interpreter -- a daemon
    on another node, an hour later -- computes the same value."""
    code = (
        "from agent_saga.idempotency import IdempotencyManager as I;"
        "print(I.key('saga-restart', 'step-9'))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, cwd=str(Path(__file__).resolve().parent.parent))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == IdempotencyManager.key("saga-restart", "step-9")


def test_attempt_count_is_not_part_of_the_key():
    """The guardrail's whole mechanism is a key the remote recognises as a
    duplicate. A key that varied per attempt would make attempt 2 look like a
    brand-new refund -- causing exactly the double charge it must prevent."""
    key = IdempotencyManager.key("saga-1", "step-1")
    for _ in range(5):
        assert IdempotencyManager.key("saga-1", "step-1") == key


def test_legacy_recovery_token_matches_the_manager():
    """Journals written by earlier versions must still be recognised."""
    assert recovery_token("s", "st") == IdempotencyManager.key("s", "st")


# ==========================================================================
# Injection into the compensation handler
# ==========================================================================

def test_key_is_injected_into_a_handler_that_accepts_it():
    kwargs = IdempotencyManager.inject(refund, {"charge_id": "ch_1"}, "KEY123")
    assert kwargs == {"charge_id": "ch_1", "idempotency_key": "KEY123"}


def test_handler_without_the_parameter_is_left_alone():
    """Injecting blindly would raise TypeError and turn a working compensation
    into a failed one."""
    kwargs = IdempotencyManager.inject(no_kwargs, {"charge_id": "ch_1"}, "KEY123")
    assert kwargs == {"charge_id": "ch_1"}


def test_var_keyword_handlers_receive_the_key():
    def handler(charge_id, **kw):
        return kw
    assert IdempotencyManager.inject(handler, {"charge_id": "c"}, "K")["idempotency_key"] == "K"


def test_a_connector_supplied_key_is_never_overwritten():
    """Stripe's refund key must match the one the original request used; ours
    is only a fallback."""
    kwargs = IdempotencyManager.inject(
        refund, {"charge_id": "ch_1", "idempotency_key": "stripe-native"}, "OURS")
    assert kwargs["idempotency_key"] == "stripe-native"


# ==========================================================================
# The execution ledger
# ==========================================================================

def _write(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for i, r in enumerate(records, start=1):
            fh.write(json.dumps({"seq": i, **r}) + "\n")


def test_completed_keys_reads_the_daemon_journal():
    with tempfile.TemporaryDirectory() as d:
        j = Path(d) / "recovery.jsonl"
        token = IdempotencyManager.key("s1", "st1")
        _write(j, [{"event": "RECOVERY_SUCCESS", "saga_id": "s1",
                    "step_id": "st1", "token": token}])
        assert token in IdempotencyManager.completed_keys(j)


def test_completed_keys_also_reads_the_crashed_process_wal():
    """The dead process's own COMPENSATED records prove work the daemon never
    did. A daemon consulting only its own journal would redo it."""
    wal_records = [{"event": "COMPENSATED", "saga_id": "s1", "step_id": "st1"}]
    keys = IdempotencyManager.completed_keys(None, wal_records)
    assert IdempotencyManager.key("s1", "st1") in keys


def test_completed_keys_tolerates_a_torn_journal_line():
    with tempfile.TemporaryDirectory() as d:
        j = Path(d) / "recovery.jsonl"
        token = IdempotencyManager.key("s1", "st1")
        with open(j, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "RECOVERY_SUCCESS", "token": token}) + "\n")
            fh.write('{"event": "RECOVERY_ATT')      # torn
        assert token in IdempotencyManager.completed_keys(j)


def test_attempts_are_counted_as_telemetry():
    with tempfile.TemporaryDirectory() as d:
        j = Path(d) / "recovery.jsonl"
        token = IdempotencyManager.key("s1", "st1")
        _write(j, [{"event": "RECOVERY_ATTEMPT", "token": token},
                   {"event": "RECOVERY_ATTEMPT", "token": token}])
        assert IdempotencyManager.attempts(j)[token] == 2


# ==========================================================================
# The retry scenario, end to end
# ==========================================================================

OLD = time.time() - 3600


def _dangling_saga(saga_id="s1", steps=2, ts=OLD):
    """A saga that ran N compensable steps and then went silent."""
    recs = [{"event": "SAGA_START", "saga_id": saga_id, "ts": ts,
             "pid": 4242, "lease_ttl": 5.0}]
    for i in range(1, steps + 1):
        recs.append({"event": "STEP_INTENT", "saga_id": saga_id, "ts": ts,
                     "step_id": f"st{i}", "tool": f"tool{i}",
                     "semantics": "COMPENSABLE", "kwargs": {}})
        recs.append({"event": "STEP_COMMITTED", "saga_id": saga_id, "ts": ts,
                     "step_id": f"st{i}", "tool": f"tool{i}",
                     "semantics": "COMPENSABLE",
                     "compensation": {"handler": "idem.refund", "recoverable": True,
                                      "kwargs": {"charge_id": f"ch_{i}"},
                                      "idempotency_key": None,
                                      "fn": "refund", "description": ""}})
    return recs


@aio
async def test_a_completed_compensation_is_not_run_twice_after_a_crash():
    """The core scenario: step 2's compensation succeeded, then the daemon died
    before finalising. The next sweep must skip it and not refund again."""
    CALLS.clear()
    with tempfile.TemporaryDirectory() as d:
        wal = Path(d) / "wal.jsonl"
        _write(wal, _dangling_saga(steps=2))

        daemon = RecoveryDaemon(wal, daemon_id="d1")
        # Simulate the crash: st2 already compensated, journalled, then death.
        daemon._journal("RECOVERY_SUCCESS", {
            "saga_id": "s1", "step_id": "st2", "tool": "tool2",
            "token": IdempotencyManager.key("s1", "st2")})

        outcome = (await RecoveryDaemon(wal, daemon_id="d2").recover_all())[0]

        assert outcome.resolution is Resolution.RECOVERED
        charged = [c["charge_id"] for c in CALLS]
        assert charged == ["ch_1"], "st2 must be skipped, only st1 compensated"


@aio
async def test_rerunning_the_daemon_over_the_same_wal_is_a_no_op():
    """Idempotency across full sweeps, not just within one."""
    CALLS.clear()
    with tempfile.TemporaryDirectory() as d:
        wal = Path(d) / "wal.jsonl"
        _write(wal, _dangling_saga(steps=2))

        first = await RecoveryDaemon(wal, daemon_id="d1").recover_all()
        assert first[0].resolution is Resolution.RECOVERED
        assert len(CALLS) == 2

        # Same WAL, fresh daemon, second sweep: nothing may run again.
        second = await RecoveryDaemon(wal, daemon_id="d2").recover_all()
        assert len(CALLS) == 2, "a second sweep must not re-refund anything"
        assert second[0].resolution is Resolution.RECOVERED


@aio
async def test_the_daemon_injects_the_deterministic_key_into_the_handler():
    CALLS.clear()
    with tempfile.TemporaryDirectory() as d:
        wal = Path(d) / "wal.jsonl"
        _write(wal, _dangling_saga(steps=1))
        await RecoveryDaemon(wal).recover_all()

    assert len(CALLS) == 1
    assert CALLS[0]["idempotency_key"] == IdempotencyManager.key("s1", "st1")


@aio
async def test_the_journal_records_the_attempt_number_without_keying_on_it():
    CALLS.clear()
    with tempfile.TemporaryDirectory() as d:
        wal = Path(d) / "wal.jsonl"
        _write(wal, _dangling_saga(steps=1))
        daemon = RecoveryDaemon(wal)
        await daemon.recover_all()

        entries = [json.loads(l) for l in
                   daemon.journal_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        attempt = next(e for e in entries if e["event"] == "RECOVERY_ATTEMPT")
        assert attempt["attempt"] == 1
        # The key handed to the handler is scope-based, never attempt-based.
        assert attempt["idempotency_key"] == IdempotencyManager.key("s1", "st1")


# ==========================================================================
# Backend-agnostic recovery
# ==========================================================================

@aio
async def test_the_daemon_recovers_from_a_wal_backend_not_just_a_file():
    """The multi-node payoff: a daemon reading a shared backend can resolve a
    saga orphaned by a process it never shared a filesystem with."""
    CALLS.clear()
    from agent_saga.wal.redis_wal import RedisWAL

    class FakeRedis:
        def __init__(self): self.lists = {}
        async def rpush(self, key, *values):
            self.lists.setdefault(key, []).extend(values)
            return len(self.lists[key])
        async def lrange(self, key, s, e): return self.lists.get(key, [])
        async def delete(self, key): self.lists.pop(key, None); return 1
        async def aclose(self): pass

    with tempfile.TemporaryDirectory() as d:
        shared = RedisWAL(client=FakeRedis(), key="fleet:wal")
        await shared.start()
        # Node A writes a saga and dies without finishing it.
        for rec in _dangling_saga(steps=1):
            shared.append(rec.pop("event"), rec)
        await shared.barrier()

        # Node B's daemon, given only the shared backend, resolves it.
        daemon = RecoveryDaemon(shared, journal_path=Path(d) / "recovery.jsonl",
                                claims_dir=Path(d) / "claims")
        outcome = (await daemon.recover_all())[0]
        await shared.close()

    assert outcome.resolution is Resolution.RECOVERED
    assert [c["charge_id"] for c in CALLS] == ["ch_1"]


@aio
async def test_sync_scan_refuses_a_backend_wal_rather_than_lying():
    from agent_saga.wal.redis_wal import RedisWAL

    class FakeRedis:
        async def lrange(self, *a): return []
        async def aclose(self): pass

    wal = RedisWAL(client=FakeRedis(), key="k")
    daemon = RecoveryDaemon(wal)
    with pytest.raises(RuntimeError, match="await scan_async"):
        daemon.scan()
