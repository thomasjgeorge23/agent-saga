"""Tests for agent-saga v0.2.0 global workflow features & engine supremacy."""

import asyncio
import pytest
from conftest import aio

from agent_saga import (
    DurableTimerManager,
    CronSagaScheduler,
    SignalBus,
    QueryBus,
    ChildSaga,
    ParallelSagaGroup,
    BPMNExporter,
    BPMNImporter,
    ReplayVerifier,
)
from agent_saga.context import SagaContext
from agent_saga.adapters.temporal import SagaTemporalInterceptor, saga_activity
from agent_saga.adapters.camunda import SagaCamundaWorker, camunda_job_handler


@aio
async def test_durable_timers(tmp_path):
    mgr = DurableTimerManager(tmp_path / "timers.json")
    timer = mgr.schedule_sleep("saga_1", 0.01)
    assert timer.timer_id is not None
    await mgr.sleep("saga_1", 0.01)
    assert len(mgr._timers) >= 1


@aio
async def test_cron_scheduler():
    runs = []

    def callback(fn_name, payload):
        runs.append(fn_name)

    sched = CronSagaScheduler(callback)
    sched.schedule_cron("job_1", "* * * * *", "daily_recon")
    await sched.start()
    await asyncio.sleep(0.3)
    await sched.stop()
    assert len(runs) >= 1
    assert runs[0] == "daily_recon"


@aio
async def test_signals_and_queries():
    sig_bus = SignalBus()
    query_bus = QueryBus()
    received = []

    sig_bus.register_handler("saga_99", "HALT", lambda msg: received.append(msg.signal_name))
    query_bus.register_query("saga_99", "status", lambda: "RUNNING")

    await sig_bus.send_signal("saga_99", "HALT", {"reason": "user_cancelled"})
    status = await query_bus.query("saga_99", "status")

    assert received == ["HALT"]
    assert status == "RUNNING"


@aio
async def test_child_saga_and_parallel_group():
    parent_ctx = SagaContext(saga_id="parent_saga")
    group = ParallelSagaGroup("fan_out", parent_ctx)

    res_list = []

    def task1(ctx):
        res_list.append("t1")
        return "ok1"

    def task2(ctx):
        res_list.append("t2")
        return "ok2"

    group.add_task(task1)
    group.add_task(task2)

    results = await group.execute_all()
    assert results == ["ok1", "ok2"]
    assert "t1" in res_list and "t2" in res_list


@aio
async def test_continue_as_new():
    ctx = SagaContext(saga_id="long_saga_1")
    await ctx.wal.start()
    new_ctx = await ctx.continue_as_new("long_saga_2")
    assert new_ctx.saga_id == "long_saga_2"


def test_bpmn_export_and_import():
    records = [
        {"event": "STEP_COMMITTED", "tool": "stripe.charge", "compensation": {"undo": "refund"}},
        {"event": "STEP_COMMITTED", "tool": "db.insert", "compensation": None},
    ]
    xml_str = BPMNExporter.to_bpmn_xml(records)
    assert "<bpmn:definitions" in xml_str
    assert "Tool: stripe.charge" in xml_str

    nodes = BPMNImporter.from_bpmn_xml(xml_str)
    assert len(nodes) >= 2


@aio
async def test_temporal_and_camunda_adapters():
    interceptor = SagaTemporalInterceptor("temp_saga")
    res = await interceptor.execute_activity("stripe_activity", lambda amount: amount * 2, 50)
    assert res == 100

    camunda_worker = SagaCamundaWorker("camunda_saga")
    res2 = await camunda_worker.execute_job("credit_check", lambda vars: vars["score"] > 700, {"score": 750})
    assert res2 is True


def test_replay_verifier():
    records = [{"event": "STEP_COMMITTED", "_h": "abc12345"}]
    res = ReplayVerifier.verify(records)
    assert res.deterministic is True
    assert res.hash_head == "abc12345"
