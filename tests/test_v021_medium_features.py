"""Tests for agent-saga v0.2.1 MEDIUM architectural features (Items 11-22)."""

import pytest
from conftest import aio

from agent_saga import (
    SagaContext,
    ParallelSagaGroup,
    PostgresApprovalStore,
    AutoLockHeartbeat,
    KeyRingEncryptor,
    SnapshotGC,
)
from agent_saga.ui.dashboard import get_saga_ui_app
from agent_saga.testing import ChaosRunner, verify_saga_replay
from agent_saga.entanglement import get_correlation_headers, HEADER_CORRELATION_ID


def test_postgres_approval_store():
    store = PostgresApprovalStore()
    from agent_saga.approvals import ApprovalRequest
    req = ApprovalRequest(id="req1", saga_id="s1", step_id="st1", tool="t1", rule="r1", reason="test")
    created = store.create(req)
    assert created.id == "req1"
    decided = store.decide("req1", granted=True, approver="admin")
    assert decided.status == "GRANTED"


def test_ui_dashboard_auth():
    app = get_saga_ui_app(token="secret-token-123")
    assert app is not None


@aio
async def test_parallel_saga_group_modes():
    parent_ctx = SagaContext(saga_id="p1")
    await parent_ctx.wal.start()
    group_fast = ParallelSagaGroup("fast_group", parent_ctx, mode="fail_fast")
    group_best = ParallelSagaGroup("best_group", parent_ctx, mode="best_effort")

    group_fast.add_task(lambda ctx: "ok1")
    res1 = await group_fast.execute_all()
    assert res1 == ["ok1"]

    def failing_task(ctx):
        raise ValueError("boom")

    group_best.add_task(lambda ctx: "ok2")
    group_best.add_task(failing_task)

    res2 = await group_best.execute_all()
    assert len(res2) == 2
    assert res2[0] == "ok2"
    assert isinstance(res2[1], ValueError)


def _make_multistep_saga(committed: list):
    """A saga of 5 COMPENSABLE steps whose compensations append their name to
    `committed`, so a test can see exactly which steps rolled back."""
    from agent_saga.semantics import ActionSemantics
    from agent_saga.context import Compensation

    async def saga(ctx):
        for i in range(1, 6):
            def make_comp(n):
                def _c(**kw):
                    committed.append(f"step{n}")
                return _c
            await ctx.execute(
                tool=f"tool{i}", semantics=ActionSemantics.COMPENSABLE,
                forward=(lambda n=i: {"step": n}),
                compensate=(lambda res, n=i: Compensation(
                    fn=make_comp(n), handler=f"comp{n}", kwargs={},
                    description=f"undo step{n}")))
    return saga


@aio
async def test_chaos_runner_fail_after_triggers_partial_rollback():
    comps = []
    runner = ChaosRunner(fail_after=2)
    res = await runner.run(_make_multistep_saga(comps))
    assert res.rolled_back is True
    assert res.injected_at == 2
    # Steps 1 and 2 committed before the injected failure -> both compensate.
    assert set(comps) == {"step1", "step2"}


@aio
async def test_chaos_runner_fail_at_matrix():
    runner = ChaosRunner(fail_at=[2, 4])
    results = {}
    for point in (2, 4):
        comps = []
        r = await runner.run(_make_multistep_saga(comps), fail_point=point)
        results[point] = (r, sorted(comps))
    # Failing at step 2 rolls back {1,2}; at step 4 rolls back {1,2,3,4}.
    assert results[2][0].injected_at == 2 and results[2][1] == ["step1", "step2"]
    assert results[4][0].injected_at == 4 and results[4][1] == ["step1", "step2", "step3", "step4"]


@aio
async def test_chaos_runner_run_all_returns_result_per_point():
    runner = ChaosRunner(fail_at=[1, 3])
    results = await runner.run_all(_make_multistep_saga([]))
    assert set(results.keys()) == {1, 3}
    assert results[1].injected_at == 1
    assert results[3].injected_at == 3


@aio
async def test_chaos_runner_completes_when_fail_point_beyond_steps():
    # 5-step saga, fail point 9 never reached -> clean completion, no rollback.
    runner = ChaosRunner(fail_after=9)
    res = await runner.run(_make_multistep_saga([]))
    assert res.rolled_back is False
    assert res.injected_at is None


def test_correlation_headers():
    ctx = SagaContext(saga_id="saga-corr-100")
    headers = get_correlation_headers(ctx)
    assert headers[HEADER_CORRELATION_ID] == "saga-corr-100"


# -- #11 approvals list --status filter ---------------------------------------

def test_approvals_list_by_status(tmp_path):
    from agent_saga.approvals import FileApprovalStore, ApprovalRequest
    from agent_saga.cli import _approvals_by_status
    store = FileApprovalStore(tmp_path)
    store.create(ApprovalRequest(id="a" * 12, saga_id="s1", step_id="x", tool="t1", rule="r", reason="p"))
    store.create(ApprovalRequest(id="b" * 12, saga_id="s2", step_id="x", tool="t2", rule="r", reason="g"))
    store.create(ApprovalRequest(id="c" * 12, saga_id="s3", step_id="x", tool="t3", rule="r", reason="d"))
    store.decide("b" * 12, granted=True, approver="alice")
    store.decide("c" * 12, granted=False, approver="bob")

    assert [r.tool for r in _approvals_by_status(store, "pending")] == ["t1"]
    assert [r.tool for r in _approvals_by_status(store, "granted")] == ["t2"]
    assert [r.tool for r in _approvals_by_status(store, "denied")] == ["t3"]
    assert sorted(r.tool for r in _approvals_by_status(store, "all")) == ["t1", "t2", "t3"]


def test_approvals_status_degrades_for_pending_only_store():
    from agent_saga.cli import _approvals_by_status

    class OnlyPending:
        def pending(self): return []

    store = OnlyPending()
    # 'pending' works on any store; history statuses return None (CLI prints a note).
    assert _approvals_by_status(store, "pending") == []
    assert _approvals_by_status(store, "granted") is None
    assert _approvals_by_status(store, "denied") is None


# -- #14 DurableTimerManager.cancel(name) -------------------------------------

@aio
async def test_durable_timer_cancel_wakes_sleeper():
    import asyncio, time
    from agent_saga.scheduler import DurableTimerManager, TimerCancelled
    m = DurableTimerManager()

    async def sleeper():
        try:
            await m.sleep("saga-1", 100.0, name="onboard-reminder")
            return "fired"
        except TimerCancelled:
            return "cancelled"

    task = asyncio.create_task(sleeper())
    await asyncio.sleep(0.05)
    t0 = time.time()
    assert m.cancel("onboard-reminder") is True          # cancel by name
    assert await task == "cancelled"
    assert time.time() - t0 < 1.0                          # woke promptly


@aio
async def test_durable_timer_cancel_unknown_and_pending(tmp_path):
    from agent_saga.scheduler import DurableTimerManager
    m = DurableTimerManager(tmp_path / "timers.json")
    assert m.cancel("does-not-exist") is False
    m.schedule_sleep("saga-2", -1.0, name="due-now")       # already due
    assert any(t.timer_id == "due-now" for t in m.list_pending())
    assert m.cancel("due-now") is True
    assert not any(t.timer_id == "due-now" for t in m.list_pending())
    assert m.cancel("due-now") is False                    # already cancelled


def test_cron_scheduler_cancel():
    from agent_saga.scheduler import CronSagaScheduler
    sched = CronSagaScheduler(callback=lambda *a: None)
    sched.schedule_cron("nightly", "0 0 * * *", "run_report")
    assert sched.cancel("nightly") is True
    assert sched.cancel("nightly") is False
