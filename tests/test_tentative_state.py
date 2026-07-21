"""Tentative state and semantic locks -- the countermeasures for the isolation
a saga does not have.

A saga commits each step as it runs, so a second reader can see a balance the
first saga has already spent but not yet confirmed. These tests pin the two
structural answers: provisional status, and a claim other sagas respect.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from agent_saga import ActionSemantics, AsyncWAL, Compensation, SagaContext, saga
from agent_saga.locks import (
    SemanticLockConflictError,
    SemanticLockManager,
    get_semantic_locks,
    set_semantic_locks,
)
from agent_saga.patterns import (
    TentativeConflictError,
    TentativeResource,
    TentativeStatus,
    tentative,
)
from conftest import aio

C = ActionSemantics.COMPENSABLE


@pytest.fixture(autouse=True)
def _fresh_locks():
    """Every test gets an empty lock table, so one test's claim cannot leak."""
    previous = get_semantic_locks()
    set_semantic_locks(SemanticLockManager())
    yield
    set_semantic_locks(previous)


async def _ctx(tmp: Path):
    wal = AsyncWAL(tmp / "wal.jsonl")
    await wal.start()
    return SagaContext(wal=wal), wal


# ==========================================================================
# Status transitions
# ==========================================================================

def test_a_new_resource_starts_pending():
    r = TentativeResource(resource_id="account:usr_1")
    assert r.status is TentativeStatus.PENDING
    assert r.is_pending and not r.resolved


@aio
async def test_commit_and_rollback_are_terminal():
    r = TentativeResource(resource_id="a")
    await r.commit()
    assert r.status is TentativeStatus.COMMITTED and r.resolved


@aio
async def test_resolving_twice_is_refused_rather_than_silently_ignored():
    """A double resolution means the lifecycle ran twice; swallowing it would
    surface much later as a wrong balance."""
    r = TentativeResource(resource_id="a")
    await r.commit()
    with pytest.raises(TentativeConflictError, match="already COMMITTED"):
        await r.rollback()


@aio
async def test_rolled_back_state_is_recorded_not_erased():
    r = TentativeResource(resource_id="a")
    await r.rollback()
    assert r.status is TentativeStatus.ROLLED_BACK   # auditable, not deleted


# ==========================================================================
# The two-step workflow
# ==========================================================================

@aio
async def test_resource_stays_pending_during_execution_then_commits():
    events = []
    with tempfile.TemporaryDirectory() as d:
        ctx, wal = await _ctx(Path(d))
        await ctx.begin()
        balance = tentative(ctx, "account:usr_1",
                            on_commit=lambda: events.append("confirmed"),
                            on_rollback=lambda: events.append("restored"))

        await ctx.execute(tool="debit", semantics=C, forward=lambda: {"id": 1},
                          compensate=lambda r: Compensation(fn=lambda: None, handler="h"))
        # Mid-saga the write is visible but explicitly provisional.
        assert balance.status is TentativeStatus.PENDING
        assert events == []

        await ctx.finish()
        await wal.close()

    assert balance.status is TentativeStatus.COMMITTED
    assert events == ["confirmed"]


@aio
async def test_failure_in_step_two_rolls_the_resource_back():
    """The headline case: step 2 fails, so the provisional debit must become
    ROLLED_BACK and the restore callback must fire."""
    events = []
    state = {"balance": 100}

    @saga(reraise=False)
    async def workflow():
        from agent_saga import current_saga
        ctx = current_saga()
        tentative(ctx, "account:usr_1",
                  on_commit=lambda: events.append("confirmed"),
                  on_rollback=lambda: state.__setitem__("balance", 100))
        state["balance"] = 40                       # step 1: tentative debit
        await ctx.execute(tool="debit", semantics=C, forward=lambda: {"id": 1},
                          compensate=lambda r: Compensation(fn=lambda: None, handler="h"))
        raise ValueError("step 2 failed")           # step 2

    report = await workflow()
    assert report.clean
    assert state["balance"] == 100                  # restored
    assert events == []                             # never confirmed


@aio
async def test_a_tentative_resource_resolves_exactly_once_across_the_boundary():
    """rollback() resolves it, and finish(aborted=True) must not try again."""
    calls = []
    with tempfile.TemporaryDirectory() as d:
        ctx, wal = await _ctx(Path(d))
        await ctx.begin()
        tentative(ctx, "r1", on_rollback=lambda: calls.append("undo"))
        await ctx.rollback()
        await ctx.finish(aborted=True, clean=True)
        await wal.close()
    assert calls == ["undo"]


@aio
async def test_a_failing_callback_is_reported_not_swallowed():
    with tempfile.TemporaryDirectory() as d:
        ctx, wal = await _ctx(Path(d))
        await ctx.begin()

        def boom():
            raise RuntimeError("ledger service down")

        tentative(ctx, "r1", on_commit=boom)
        await ctx.finish()
        await wal.close()
        events = [r["event"] for r in wal.records()]

    # A resource stuck PENDING is a balance nobody reconciles -- it must appear.
    assert "TENTATIVE_UNRESOLVED" in events


# ==========================================================================
# Semantic locks
# ==========================================================================

@aio
async def test_a_second_saga_cannot_claim_a_locked_resource():
    with tempfile.TemporaryDirectory() as d:
        a, wal_a = await _ctx(Path(d) / "a")
        b, wal_b = await _ctx(Path(d) / "b")
        await a.begin()
        await b.begin()

        tentative(a, "account:usr_1", lock=True)
        with pytest.raises(SemanticLockConflictError, match="semantically locked"):
            tentative(b, "account:usr_1", lock=True)

        await a.finish()
        await b.finish()
        await wal_a.close()
        await wal_b.close()


@aio
async def test_the_lock_is_released_when_the_saga_finishes_so_the_next_one_proceeds():
    with tempfile.TemporaryDirectory() as d:
        a, wal_a = await _ctx(Path(d) / "a")
        await a.begin()
        tentative(a, "account:usr_1", lock=True)
        assert get_semantic_locks().owner("account:usr_1") == a.saga_id
        await a.finish()
        await wal_a.close()

        assert get_semantic_locks().owner("account:usr_1") is None

        b, wal_b = await _ctx(Path(d) / "b")
        await b.begin()
        tentative(b, "account:usr_1", lock=True)     # now free
        await b.finish()
        await wal_b.close()


@aio
async def test_locks_are_released_even_when_the_saga_aborts():
    """An aborted saga must not strand a resource claimed forever."""
    resource = "account:usr_1"

    @saga(reraise=False)
    async def failing():
        from agent_saga import current_saga
        tentative(current_saga(), resource, lock=True)
        raise ValueError("boom")

    await failing()
    assert get_semantic_locks().owner(resource) is None


@aio
async def test_the_same_saga_may_reacquire_its_own_lock():
    """A multi-step workflow naturally touches one account more than once."""
    with tempfile.TemporaryDirectory() as d:
        ctx, wal = await _ctx(Path(d))
        await ctx.begin()
        await ctx.acquire_semantic_lock("account:usr_1")
        await ctx.acquire_semantic_lock("account:usr_1")   # re-entrant
        await ctx.finish()
        await wal.close()


@aio
async def test_acquire_can_wait_for_a_configurable_timeout():
    mgr = get_semantic_locks()
    assert mgr.try_acquire("r", "saga-A")

    async def release_soon():
        await asyncio.sleep(0.05)
        mgr.release("r", "saga-A")

    asyncio.create_task(release_soon())
    await mgr.acquire("r", "saga-B", timeout=2.0)      # waits, then succeeds
    assert mgr.owner("r") == "saga-B"


@aio
async def test_waiting_gives_up_and_raises_rather_than_hanging():
    mgr = get_semantic_locks()
    mgr.try_acquire("r", "saga-A")
    with pytest.raises(SemanticLockConflictError):
        await mgr.acquire("r", "saga-B", timeout=0.05)


def test_one_saga_cannot_release_anothers_claim():
    mgr = get_semantic_locks()
    mgr.try_acquire("r", "saga-A")
    assert mgr.release("r", "saga-B") is False
    assert mgr.owner("r") == "saga-A"


@aio
async def test_concurrent_sagas_contend_for_one_resource_and_only_one_wins():
    winners, losers = [], []

    @saga(reraise=False)
    async def contend(name):
        from agent_saga import current_saga
        ctx = current_saga()
        try:
            tentative(ctx, "account:shared", lock=True)
            winners.append(name)
            await asyncio.sleep(0.05)
        except SemanticLockConflictError:
            losers.append(name)

    await asyncio.gather(contend("a"), contend("b"))
    assert len(winners) == 1 and len(losers) == 1
    assert get_semantic_locks().owner("account:shared") is None   # cleaned up


# ==========================================================================
# Crash durability -- a tentative resource must survive the crash the whole
# engine exists for
# ==========================================================================

@aio
async def test_a_tentative_stranded_by_a_real_crash_is_rolled_back_by_the_daemon():
    """Registration lives in the WAL, not just memory, so a daemon in another
    process can find the stranded resource and settle it."""
    import subprocess
    import sys as _sys
    import time as _time

    from agent_saga.recovery import RecoveryDaemon, Resolution
    from agent_saga.registry import compensator

    @compensator("demo.restore_balance")
    def restore_balance(account, path, idempotency_key=None):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"restored:{account}\n")

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        wal = tmp / "wal.jsonl"
        effects = tmp / "effects.txt"
        worker = Path(__file__).parent / "tentative_crash_worker.py"

        proc = subprocess.run(
            [_sys.executable, str(worker), str(wal), str(effects), "account:usr_1"],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 9, proc.stderr
        assert effects.read_text(encoding="utf-8").strip() == "debited:account:usr_1"

        _time.sleep(0.8)          # let the lease expire
        outcome = (await RecoveryDaemon(wal).recover_all())[0]

        assert outcome.resolution is Resolution.RECOVERED
        lines = effects.read_text(encoding="utf-8").splitlines()
        assert lines == ["debited:account:usr_1", "restored:account:usr_1"]


@aio
async def test_an_in_process_only_tentative_escalates_instead_of_being_guessed_at():
    """No registry handler means no daemon can settle it. Escalate, loudly."""
    import json as _json
    import time as _time

    from agent_saga.recovery import RecoveryDaemon, Resolution

    with tempfile.TemporaryDirectory() as d:
        wal = Path(d) / "wal.jsonl"
        old = _time.time() - 3600
        recs = [
            {"seq": 1, "event": "SAGA_START", "saga_id": "s1", "ts": old,
             "pid": 1, "lease_ttl": 5.0},
            {"seq": 2, "event": "TENTATIVE_REGISTERED", "saga_id": "s1", "ts": old,
             "resource_id": "account:usr_9", "status": "PENDING",
             "rollback_handler": None, "recoverable": False, "rollback_kwargs": {}},
        ]
        with open(wal, "w", encoding="utf-8") as fh:
            for r in recs:
                fh.write(_json.dumps(r) + "\n")

        outcome = (await RecoveryDaemon(wal).recover_all())[0]
        assert outcome.resolution is Resolution.NEEDS_HUMAN
        assert "account:usr_9" in outcome.escalated
        assert "in-process only" in outcome.reason


# ==========================================================================
# Distributed semantic locks
# ==========================================================================

class FakeRedisLocks:
    """The slice of redis.asyncio RedisSemanticLocks uses: SET NX PX, GET, EVAL."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.calls: list[tuple] = []

    async def set(self, key, value, nx=False, px=None):
        self.calls.append(("set", key, value, nx, px))
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(key)

    async def eval(self, script, numkeys, key, *args):
        token = args[0]
        self.calls.append(("eval", key, token))
        if "pexpire" in script:                       # renewal
            return 1 if self.store.get(key) == token else 0
        if self.store.get(key) == token:              # compare-and-delete
            del self.store[key]
            return 1
        return 0

    async def aclose(self):
        pass


def _redis_locks(**kw):
    from agent_saga.locks import RedisSemanticLocks

    return RedisSemanticLocks(client=FakeRedisLocks(), **kw)


@aio
async def test_distributed_lock_is_exclusive_across_two_nodes():
    """The in-memory manager cannot do this: two pods would each 'acquire' and
    neither would learn of the other."""
    from agent_saga.locks import RedisSemanticLocks

    shared = FakeRedisLocks()
    node_a = RedisSemanticLocks(client=shared)
    node_b = RedisSemanticLocks(client=shared)

    await node_a.acquire("account:usr_1", "saga-A")
    with pytest.raises(SemanticLockConflictError, match="semantically locked"):
        await node_b.acquire("account:usr_1", "saga-B")

    await node_a.release("account:usr_1", "saga-A")
    await node_b.acquire("account:usr_1", "saga-B")     # now free
    assert await node_b.owner("account:usr_1") == "saga-B"
    await node_a.close()
    await node_b.close()


@aio
async def test_distributed_acquire_sets_an_expiry_so_a_dead_holder_cannot_deadlock():
    locks = _redis_locks(ttl_ms=15_000)
    await locks.acquire("r", "saga-A")
    call = next(c for c in locks._client.calls if c[0] == "set")
    assert call[3] is True and call[4] == 15_000       # NX and PX both set
    await locks.close()


@aio
async def test_release_is_compare_and_delete_not_a_bare_del():
    """If our TTL lapsed and another saga took the lock, a bare DEL would free
    THEIR claim. The Lua CAS makes that impossible."""
    shared = FakeRedisLocks()
    from agent_saga.locks import RedisSemanticLocks

    a = RedisSemanticLocks(client=shared)
    await a.acquire("r", "saga-A")

    shared.store["agent-saga:semlock:r"] = "saga-B"    # our lease lapsed; B took it
    freed = await a.release("r", "saga-A")

    assert freed is False
    assert shared.store["agent-saga:semlock:r"] == "saga-B"   # B keeps its claim
    await a.close()


@aio
async def test_distributed_lock_is_reentrant_for_the_same_saga():
    locks = _redis_locks()
    await locks.acquire("r", "saga-A")
    await locks.acquire("r", "saga-A")                # must not conflict
    await locks.close()


@aio
async def test_distributed_acquire_can_wait_for_a_timeout():
    shared = FakeRedisLocks()
    from agent_saga.locks import RedisSemanticLocks

    a = RedisSemanticLocks(client=shared)
    b = RedisSemanticLocks(client=shared)
    await a.acquire("r", "saga-A")

    async def free_soon():
        await asyncio.sleep(0.05)
        await a.release("r", "saga-A")

    asyncio.create_task(free_soon())
    await b.acquire("r", "saga-B", timeout=2.0, poll=0.01)
    assert await b.owner("r") == "saga-B"
    await a.close()
    await b.close()


@aio
async def test_release_all_frees_every_claim_a_saga_holds():
    locks = _redis_locks()
    await locks.acquire("r1", "saga-A")
    await locks.acquire("r2", "saga-A")
    await locks.acquire("r3", "saga-B")
    freed = await locks.release_all("saga-A")
    assert sorted(freed) == ["r1", "r2"]
    assert await locks.owner("r3") == "saga-B"        # untouched
    await locks.close()


@aio
async def test_sync_acquire_on_a_distributed_backend_refuses_with_guidance():
    """Returning True would hand back a claim that was never made. Refuse, and
    name the async call to use instead."""
    locks = _redis_locks()
    with pytest.raises(RuntimeError, match="cannot acquire synchronously"):
        locks.try_acquire("r", "saga-A")
    await locks.close()


@aio
async def test_tentative_with_lock_true_refuses_on_a_distributed_backend():
    locks = _redis_locks()
    set_semantic_locks(locks)
    try:
        with tempfile.TemporaryDirectory() as d:
            ctx, wal = await _ctx(Path(d))
            await ctx.begin()
            with pytest.raises(RuntimeError, match="acquire_semantic_lock"):
                tentative(ctx, "account:usr_1", lock=True)
            await ctx.finish()
            await wal.close()
    finally:
        await locks.close()


@aio
async def test_the_async_path_works_end_to_end_with_a_distributed_backend():
    """The supported pattern: await the lock, then register the resource."""
    locks = _redis_locks()
    set_semantic_locks(locks)
    try:
        with tempfile.TemporaryDirectory() as d:
            ctx, wal = await _ctx(Path(d))
            await ctx.begin()
            await ctx.acquire_semantic_lock("account:usr_1")
            tentative(ctx, "account:usr_1", lock=False)
            assert await locks.owner("account:usr_1") == ctx.saga_id
            await ctx.finish()
            await wal.close()
            # Released at the boundary, through the async path.
            assert await locks.owner("account:usr_1") is None
    finally:
        await locks.close()
