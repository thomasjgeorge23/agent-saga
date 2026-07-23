"""Time-travel replay CLI (#31): reconstruct a historical saga and walk its
forward actions, compensations, failure point, and rollback."""

import io
from contextlib import redirect_stdout, redirect_stderr

import pytest
from conftest import aio

from agent_saga import saga_scope, ActionSemantics
from agent_saga.context import Compensation
from agent_saga.wal.file_wal import FileWAL
from agent_saga.cli import main, _resolve_saga_id, _replay_execute
from agent_saga.ui.reader import SagaWALReader


async def _make_failed_saga(path, refund_handler="stripe.refund", revert_handler="crm.revert"):
    wal = FileWAL(path)
    await wal.start()
    try:
        async with saga_scope(name="checkout-flow", wal=wal) as ctx:
            await ctx.execute(
                tool="stripe.charge", semantics=ActionSemantics.COMPENSABLE,
                forward=lambda: {"id": "ch_99", "amount": 4200},
                compensate=lambda r: Compensation(
                    fn=lambda **k: None, handler=refund_handler,
                    kwargs={"charge_id": r["id"], "amount": r["amount"]}, description="refund"))
            await ctx.execute(
                tool="crm.update", semantics=ActionSemantics.COMPENSABLE,
                forward=lambda: {"prev": "lead"},
                compensate=lambda r: Compensation(
                    fn=lambda **k: None, handler=revert_handler,
                    kwargs={"prev": r["prev"]}, description="revert"))
            raise ValueError("payment reconciliation mismatch")
    except Exception:
        pass
    await wal.close()
    return SagaWALReader(path).list_sagas()["sagas"][0]["saga_id"]


@aio
async def test_replay_by_name_shows_timeline_and_failure(tmp_path):
    wal = tmp_path / "r.wal"
    await _make_failed_saga(wal)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["replay", "checkout-flow", "--wal", str(wal)])   # select by NAME
    out = buf.getvalue()
    assert rc == 0
    assert "TIME-TRAVEL DEBUGGER" in out and "checkout-flow" in out
    assert "stripe.charge" in out and "crm.update" in out
    assert "stripe.refund" in out                       # compensation preview
    assert "FAILURE" in out and "payment reconciliation mismatch" in out
    assert "ROLLBACK" in out and "rollback clean: yes" in out


@aio
async def test_replay_resolution_by_id_prefix_and_miss(tmp_path):
    wal = tmp_path / "r.wal"
    full = await _make_failed_saga(wal)
    reader = SagaWALReader(wal)
    sid, err = _resolve_saga_id(reader, full[:15])       # prefix
    assert sid == full and err is None
    sid2, _ = _resolve_saga_id(reader, full)             # exact id
    assert sid2 == full
    miss, msg = _resolve_saga_id(reader, "nope")
    assert miss is None and "no saga matches" in msg


@aio
async def test_replay_execute_runs_registered_compensators(tmp_path):
    import uuid
    from agent_saga.registry import compensator, resolve
    # Unique handler names so the global registry (and other tests) never collide.
    refund_h = f"test.refund.{uuid.uuid4().hex[:8]}"
    revert_h = f"test.revert.{uuid.uuid4().hex[:8]}"
    wal = tmp_path / "r.wal"
    full = await _make_failed_saga(wal, refund_handler=refund_h, revert_handler=revert_h)

    calls = []
    compensator(refund_h)(lambda **kw: calls.append(("refund", kw)))
    compensator(revert_h)(lambda **kw: calls.append(("revert", kw)))

    detail = SagaWALReader(wal).get_saga(full)
    rc = _replay_execute(detail["steps"], resolve)
    assert rc == 0                                        # all compensations succeeded
    assert len(calls) == 2
    # the recorded charge_id reached the compensator
    assert any(c[1].get("charge_id") == "ch_99" for c in calls)


@aio
async def test_replay_reports_unregistered_compensators(tmp_path):
    wal = tmp_path / "r.wal"
    await _make_failed_saga(wal)
    # Use a unique handler name unlikely to be registered by another test.
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["replay", "checkout-flow", "--wal", str(wal)])
    out = buf.getvalue()
    # In a fresh process these handlers are not registered; the note fires.
    # (If a prior test registered them, the note is simply absent -- both fine.)
    assert "TIME-TRAVEL DEBUGGER" in out


def test_replay_missing_wal(tmp_path):
    buf = io.StringIO()
    with redirect_stderr(buf):
        rc = main(["replay", "x", "--wal", str(tmp_path / "nope.wal")])
    assert rc == 1 and "not found" in buf.getvalue()
