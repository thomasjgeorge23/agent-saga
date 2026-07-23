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


# -- #15 unified `agent-saga studio` launcher ---------------------------------

def test_studio_parser_accepts_all_services():
    from agent_saga.cli import build_parser
    args = build_parser().parse_args(
        ["studio", "--wal", "x.wal", "--port", "9099",
         "--recover", "--tail", "--dry-run", "--recover-interval", "2"])
    assert args.recover and args.tail and args.dry_run
    assert args.port == 9099 and args.recover_interval == 2.0


@aio
async def test_studio_wal_tail_streams_events(tmp_path):
    import threading, io, time, asyncio
    from contextlib import redirect_stdout
    from agent_saga.cli import _tail_wal
    from agent_saga.wal.file_wal import FileWAL

    path = tmp_path / "tail.wal"
    buf, stop = io.StringIO(), threading.Event()

    def run():
        with redirect_stdout(buf):
            _tail_wal(str(path), stop)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        time.sleep(0.4)
        wal = FileWAL(path)
        await wal.start()
        wal.append("SAGA_START", {"saga_id": "s1", "name": "onboard-acme"})
        await wal.barrier()
        await wal.close()
        time.sleep(0.9)
    finally:
        stop.set()
        t.join(timeout=2)
    out = buf.getvalue()
    assert "SAGA_START" in out and "onboard-acme" in out


# -- #19 PostgresApprovalStore connection pooling -----------------------------

class _FakePool:
    """Minimal psycopg2-style pool over an in-memory dict, so the SQL path and
    connection borrow/return balance can be tested without a real database."""
    def __init__(self):
        from agent_saga.approvals import _PG_COLUMNS
        self.rows = {}
        self.get_calls = 0
        self.put_calls = 0
        self._status_idx = _PG_COLUMNS.index("status")
        self._conn = self._Conn(self)

    class _Conn:
        def __init__(self, pool): self.pool = pool
        def cursor(self): return _FakePool._Cursor(self.pool)
        def commit(self): pass

    class _Cursor:
        def __init__(self, pool): self.pool = pool; self._res = None
        def execute(self, sql, params):
            s = " ".join(sql.split()); rows = self.pool.rows; si = self.pool._status_idx
            if s.startswith("CREATE TABLE"): self._res = []
            elif s.startswith("INSERT"):
                rows.setdefault(params[0], tuple(params)); self._res = []
            elif s.startswith("SELECT") and "WHERE id" in s:
                r = rows.get(params[0]); self._res = [r] if r else []
            elif s.startswith("SELECT"):
                self._res = [r for r in rows.values() if r[si] == params[0]]
            elif s.startswith("UPDATE"):
                r = rows.get(params[5])
                if r and r[si] == params[6]:
                    r = list(r); r[si] = params[0]; r[12] = params[1]
                    r[14] = params[2]; r[15] = params[3]; r[13] = params[4]
                    rows[params[5]] = tuple(r)
                self._res = []
        def fetchall(self): return self._res
        def close(self): pass

    def getconn(self): self.get_calls += 1; return self._conn
    def putconn(self, c): self.put_calls += 1


def test_postgres_store_from_pool_roundtrip_and_no_leak():
    from agent_saga.approvals import PostgresApprovalStore, ApprovalRequest
    pool = _FakePool()
    store = PostgresApprovalStore.from_pool(pool)

    req = ApprovalRequest(id="r1", saga_id="s1", step_id="st1", tool="stripe.charge",
                          rule="high", reason="big", saga_name="onboard")
    store.create(req)
    assert store.get("r1").tool == "stripe.charge"
    assert len(store.pending()) == 1

    dec = store.decide("r1", granted=True, approver="alice", note="ok")
    assert dec.status == "GRANTED" and dec.approver == "alice"
    assert store.pending() == []
    # first-decision-wins
    store.decide("r1", granted=False, approver="bob")
    assert store.get("r1").status == "GRANTED"
    # every borrowed connection was returned -> no pool exhaustion
    assert pool.get_calls == pool.put_calls and pool.get_calls > 0


def test_postgres_store_returns_connection_on_error():
    from agent_saga.approvals import PostgresApprovalStore

    class BoomConn:
        def cursor(self): raise RuntimeError("db exploded")
        def commit(self): pass

    class BoomPool:
        def __init__(self): self.get = 0; self.put = 0; self._c = BoomConn()
        def getconn(self): self.get += 1; return self._c
        def putconn(self, c): self.put += 1

    p = BoomPool()
    store = PostgresApprovalStore.from_pool(p)   # schema attempt swallows error
    try:
        store.get("x")
    except Exception:
        pass
    assert p.get == p.put and p.get >= 1          # returned despite the failure


def test_postgres_store_in_memory_fallback_without_db():
    from agent_saga.approvals import PostgresApprovalStore, ApprovalRequest
    store = PostgresApprovalStore()               # no connection, no pool
    store.create(ApprovalRequest(id="m1", saga_id="s", step_id="x", tool="t",
                                 rule="r", reason="z"))
    assert store.get("m1").id == "m1"
    assert store.decide("m1", granted=True, approver="a").status == "GRANTED"


# -- #20 ParallelSagaGroup fail_fast vs fail_all distinction -------------------

@aio
async def test_parallel_fail_fast_cancels_siblings():
    import asyncio
    parent = SagaContext(saga_id="p-ff")
    await parent.wal.start()
    g = ParallelSagaGroup("ff", parent, mode="fail_fast")
    sibling = {"done": False}

    async def failing(ctx):
        await asyncio.sleep(0.05); raise ValueError("boom")
    async def long_sibling(ctx):
        await asyncio.sleep(0.6); sibling["done"] = True

    g.add_task(failing); g.add_task(long_sibling)
    with pytest.raises(ValueError):
        await g.execute_all()
    # fail_fast cancelled the long sibling instead of waiting it out.
    assert sibling["done"] is False


@aio
async def test_parallel_fail_all_waits_for_all_then_raises():
    import asyncio
    parent = SagaContext(saga_id="p-fa")
    await parent.wal.start()
    g = ParallelSagaGroup("fa", parent, mode="fail_all")
    sibling = {"done": False}

    async def failing(ctx):
        await asyncio.sleep(0.05); raise ValueError("boom")
    async def long_sibling(ctx):
        await asyncio.sleep(0.3); sibling["done"] = True

    g.add_task(failing); g.add_task(long_sibling)
    with pytest.raises(ValueError):
        await g.execute_all()
    # fail_all let the sibling finish before rolling back and raising.
    assert sibling["done"] is True


# -- #21 OTLPExporter batching + flush interval -------------------------------

def test_otlp_batch_size_triggers_flush_and_chunks():
    from agent_saga.observability.otlp import OTLPExporter
    posts = []
    exp = OTLPExporter(batch_size=3)
    exp._post = lambda spans: (posts.append(len(spans)), True)[1]
    for i in range(7):
        exp.create_span(f"s{i}", "saga-1")
    # flushed at 3 and 6 spans; 1 remains buffered
    assert posts == [3, 3] and len(exp.spans) == 1
    exp.export()
    assert posts == [3, 3, 1] and exp.spans == []


def test_otlp_rebuffers_unsent_on_failure():
    from agent_saga.observability.otlp import OTLPExporter
    exp = OTLPExporter(batch_size=2)
    exp._post = lambda spans: False        # collector unreachable
    for i in range(4):
        exp.create_span(f"s{i}", "saga-2")
    # nothing dropped: all spans still buffered for the next flush
    assert len(exp.spans) == 4


def test_otlp_background_timer_flushes():
    import time
    from agent_saga.observability.otlp import OTLPExporter
    flushed = []
    exp = OTLPExporter(batch_size=1000, flush_interval_ms=80)
    exp._post = lambda spans: (flushed.append(len(spans)), True)[1]
    with exp:                              # context manager starts/stops timer
        exp.create_span("a", "s")
        exp.create_span("b", "s")
        time.sleep(0.3)
    assert sum(flushed) >= 2               # timer shipped the spans


# -- #22 setup_telemetry auto-detects existing OTEL provider ------------------

def test_setup_telemetry_provider_selection():
    import types
    from agent_saga.observability.otel import _select_provider, _provider_is_configured

    class ProxyTracerProvider: pass       # API placeholder
    class SdkTracerProvider: pass         # SDK-style provider
    class DatadogProvider: pass           # third-party, already configured

    class FakeOT:
        def __init__(self, cur): self._cur = cur; self.set_to = None
        def get_tracer_provider(self): return self._cur
        def set_tracer_provider(self, p): self.set_to = p; self._cur = p

    fake_sdk = types.SimpleNamespace(TracerProvider=SdkTracerProvider)

    # explicit wins
    _, how = _select_provider(FakeOT(ProxyTracerProvider()), "X", fake_sdk)
    assert how == "explicit"
    # an already-configured provider is attached to, not replaced
    ot = FakeOT(DatadogProvider())
    p, how = _select_provider(ot, None, fake_sdk)
    assert how == "existing" and isinstance(p, DatadogProvider) and ot.set_to is None
    # unconfigured + SDK available -> create and install globally
    ot = FakeOT(ProxyTracerProvider())
    p, how = _select_provider(ot, None, fake_sdk)
    assert how == "created" and isinstance(p, SdkTracerProvider) and ot.set_to is p
    # unconfigured + no SDK -> default, never raises
    _, how = _select_provider(FakeOT(ProxyTracerProvider()), None, None)
    assert how == "default"

    assert _provider_is_configured(DatadogProvider()) is True
    assert _provider_is_configured(ProxyTracerProvider()) is False


def test_setup_telemetry_never_raises():
    # An observability dependency must never take down the engine: setup returns a
    # usable tracer whether or not OpenTelemetry is installed.
    from agent_saga.observability.otel import setup_telemetry
    tracer = setup_telemetry()
    assert tracer is not None and hasattr(tracer, "span")
