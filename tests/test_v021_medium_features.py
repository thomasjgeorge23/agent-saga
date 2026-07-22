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


@aio
async def test_chaos_runner():
    async def sample_saga(ctx):
        await ctx.wal.append("STEP_COMMITTED", {"saga_id": ctx.saga_id, "tool": "step1"})

    runner = ChaosRunner(fail_after=1)
    res = await runner.run(sample_saga)
    assert res.rolled_back is True


def test_correlation_headers():
    ctx = SagaContext(saga_id="saga-corr-100")
    headers = get_correlation_headers(ctx)
    assert headers[HEADER_CORRELATION_ID] == "saga-corr-100"
