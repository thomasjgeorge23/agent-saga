"""Reconciliation: check that what the log claims actually happened.

The tests are written around the cases where a 200 lied -- because if the API
response were trustworthy, this module would not need to exist.
"""

import pytest

from conftest import aio

from agent_saga.reconcile import (
    CONFIRMED,
    DRIFT,
    INDETERMINATE,
    PRESENT,
    REVERSED,
    UNVERIFIABLE,
    Observation,
    Reconciliation,
    clear_reconcilers,
    expectations,
    reconciler,
    registered_reconcilers,
)
from agent_saga.wal import FileWAL


@pytest.fixture(autouse=True)
def clean_registry():
    clear_reconcilers()
    yield
    clear_reconcilers()


def comp(handler="stripe.refund", **kwargs):
    return {"handler": handler, "kwargs": kwargs or {"charge_id": "ch_1"},
            "recoverable": True}


def committed(saga="s1", step="st1", tool="stripe.charge", **kw):
    return {"event": "STEP_COMMITTED", "saga_id": saga, "step_id": step,
            "tool": tool, "compensation": comp(**kw)}


def compensated(saga="s1", step="st1", tool="stripe.charge"):
    return {"event": "COMPENSATED", "saga_id": saga, "step_id": step, "tool": tool}


# ---------------------------------------------------------------------------
# 1. What the log says should be true
# ---------------------------------------------------------------------------

def test_a_compensated_step_expects_the_effect_reversed():
    effects = expectations([committed(), compensated()])
    assert len(effects) == 1 and effects[0].expected == REVERSED


def test_the_terminal_record_wins_not_the_first():
    """Collecting every STEP_COMMITTED would assert the opposite of the truth
    on every rolled-back saga."""
    effects = expectations([committed(), compensated(), ])
    assert effects[0].expected == REVERSED

    effects = expectations([committed()])
    assert effects[0].expected == PRESENT


def test_a_failed_compensation_expects_the_effect_still_standing():
    effects = expectations([
        committed(),
        {"event": "COMPENSATION_FAILED", "saga_id": "s1", "step_id": "st1",
         "tool": "stripe.charge"}])
    assert effects[0].expected == PRESENT


def test_an_orphaned_step_expects_the_effect_standing():
    effects = expectations([
        committed(tool="wire.send"),
        {"event": "STEP_ORPHANED", "saga_id": "s1", "step_id": "st1",
         "tool": "wire.send"}])
    assert effects[0].expected == PRESENT


def test_a_timed_out_step_is_indeterminate():
    effects = expectations([
        {"event": "STEP_UNKNOWN", "saga_id": "s1", "step_id": "st1",
         "tool": "stripe.charge", "compensation": comp()}])
    assert effects[0].expected == INDETERMINATE


def test_records_without_a_step_are_ignored():
    assert expectations([{"event": "SAGA_START", "saga_id": "s1"},
                         {"event": "COMPENSATED"}]) == []


# ---------------------------------------------------------------------------
# 2. The case this module exists for
# ---------------------------------------------------------------------------

@aio
async def test_a_refund_that_never_landed_is_caught():
    """The API returned 200, the log says COMPENSATED, and the charge is still
    standing. Everything else in this library would call that clean."""
    @reconciler("stripe.refund")
    def observe(*, charge_id, **kw):
        return Observation(reversed_=False, exists=True, detail="status=succeeded")

    report = await Reconciliation().run([committed(), compensated()])

    assert not report.clean
    assert len(report.drift) == 1
    assert "still shows the original effect" in report.drift[0].detail


@aio
async def test_a_refund_that_did_land_is_confirmed():
    @reconciler("stripe.refund")
    def observe(*, charge_id, **kw):
        return Observation(reversed_=True, detail="refunded")

    report = await Reconciliation().run([committed(), compensated()])
    assert report.clean and len(report.confirmed) == 1


@aio
async def test_an_effect_reversed_outside_the_system_is_drift():
    """Somebody refunded it by hand. The log has no compensation, so the books
    and reality disagree in the other direction."""
    @reconciler("stripe.refund")
    def observe(*, charge_id, **kw):
        return Observation(reversed_=True)

    report = await Reconciliation().run([committed()])
    assert len(report.drift) == 1
    assert "no successful compensation" in report.drift[0].detail


@aio
async def test_an_effect_the_system_has_never_heard_of_is_drift():
    @reconciler("stripe.refund")
    def observe(*, charge_id, **kw):
        return Observation(exists=False, reversed_=False)

    report = await Reconciliation().run([committed()])
    assert len(report.drift) == 1
    assert "no record of it" in report.drift[0].detail


# ---------------------------------------------------------------------------
# 3. Resolving UNKNOWN -- the hardest state in the engine
# ---------------------------------------------------------------------------

def unknown_step():
    return {"event": "STEP_UNKNOWN", "saga_id": "s1", "step_id": "st1",
            "tool": "stripe.charge", "compensation": comp()}


@aio
async def test_a_timed_out_charge_that_never_landed_is_resolved():
    @reconciler("stripe.refund")
    def observe(*, charge_id, **kw):
        return Observation(exists=False)

    report = await Reconciliation().run([unknown_step()])
    assert report.clean
    assert "did NOT land" in report.confirmed[0].detail


@aio
async def test_a_timed_out_charge_that_landed_and_stands_is_drift():
    """This is the money case: the card was charged, the process died believing
    it had not been, and nothing compensated it."""
    @reconciler("stripe.refund")
    def observe(*, charge_id, **kw):
        return Observation(exists=True, reversed_=False, amount=4200)

    report = await Reconciliation().run([unknown_step()])
    assert len(report.drift) == 1
    assert "DID land" in report.drift[0].detail
    assert "amount=4200" in report.drift[0].detail


@aio
async def test_a_timed_out_charge_that_was_compensated_is_confirmed():
    @reconciler("stripe.refund")
    def observe(*, charge_id, **kw):
        return Observation(exists=True, reversed_=True)

    report = await Reconciliation().run([unknown_step()])
    assert report.clean


# ---------------------------------------------------------------------------
# 4. Unverifiable is never confirmed
# ---------------------------------------------------------------------------

@aio
async def test_an_effect_with_no_reconciler_is_unverifiable_not_fine():
    report = await Reconciliation().run([committed(), compensated()])
    assert report.checked == 1
    assert len(report.unverifiable) == 1
    assert report.confirmed == []
    assert not report.clean, "unverifiable must not report as clean"
    assert "no reconciler registered" in report.unverifiable[0].detail


@aio
async def test_a_reconciler_that_raises_is_unverifiable():
    @reconciler("stripe.refund")
    def observe(**kw):
        raise ConnectionError("stripe is unreachable")

    report = await Reconciliation().run([committed(), compensated()])
    assert len(report.unverifiable) == 1
    assert "ConnectionError" in report.unverifiable[0].detail


@aio
async def test_a_reconciler_that_hangs_is_unverifiable():
    import asyncio

    @reconciler("stripe.refund")
    async def observe(**kw):
        await asyncio.sleep(5)

    report = await Reconciliation(timeout=0.05).run([committed(), compensated()])
    assert len(report.unverifiable) == 1
    assert "timed out" in report.unverifiable[0].detail


@aio
async def test_a_system_that_cannot_tell_us_is_unverifiable():
    """A tri-state observation exists so "I don't know" survives; a boolean
    would force it into a claim."""
    @reconciler("stripe.refund")
    def observe(**kw):
        return Observation()

    report = await Reconciliation().run([committed(), compensated()])
    assert len(report.unverifiable) == 1
    assert "could not determine" in report.unverifiable[0].detail


@aio
async def test_a_reconciler_returning_the_wrong_type_is_unverifiable():
    @reconciler("stripe.refund")
    def observe(**kw):
        return True

    report = await Reconciliation().run([committed(), compensated()])
    assert len(report.unverifiable) == 1
    assert "expected Observation" in report.unverifiable[0].detail


# ---------------------------------------------------------------------------
# 5. Mechanics
# ---------------------------------------------------------------------------

@aio
async def test_async_and_sync_reconcilers_both_work():
    @reconciler("a.handler")
    def sync_observer(**kw):
        return Observation(reversed_=True)

    @reconciler("b.handler")
    async def async_observer(**kw):
        return Observation(reversed_=True)

    records = [
        committed(saga="s1", step="st1", handler="a.handler"), compensated("s1", "st1"),
        committed(saga="s2", step="st2", handler="b.handler"), compensated("s2", "st2"),
    ]
    report = await Reconciliation().run(records)
    assert len(report.confirmed) == 2 and report.clean


@aio
async def test_the_reconciler_receives_the_compensation_kwargs():
    seen = {}

    @reconciler("stripe.refund")
    def observe(**kwargs):
        seen.update(kwargs)
        return Observation(reversed_=True)

    await Reconciliation().run([
        committed(charge_id="ch_999", credential_ref="stripe_prod"),
        compensated()])
    assert seen == {"charge_id": "ch_999", "credential_ref": "stripe_prod"}


@aio
async def test_findings_are_written_back_to_the_wal(tmp_path):
    from agent_saga.integrity import verify

    @reconciler("stripe.refund")
    def observe(**kw):
        return Observation(reversed_=False, exists=True)

    wal = FileWAL(tmp_path / "w.jsonl")
    await wal.start()
    report = await Reconciliation(wal=wal).run([committed(), compensated()])
    await wal.barrier()
    records = wal.records()
    await wal.close()

    assert [r["event"] for r in records] == ["RECONCILE_DRIFT"]
    assert records[0]["tool"] == "stripe.charge"
    assert verify(records).intact


@aio
async def test_many_effects_are_checked_concurrently_but_bounded():
    import asyncio

    live = {"now": 0, "peak": 0}

    @reconciler("stripe.refund")
    async def observe(**kw):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.01)
        live["now"] -= 1
        return Observation(reversed_=True)

    records = []
    for i in range(30):
        records += [committed(saga=f"s{i}", step=f"st{i}"), compensated(f"s{i}", f"st{i}")]

    report = await Reconciliation(concurrency=4).run(records)
    assert len(report.confirmed) == 30
    assert live["peak"] <= 4, "concurrency limit ignored"


@aio
async def test_nothing_to_reconcile_is_reported_honestly():
    report = await Reconciliation().run([{"event": "SAGA_START", "saga_id": "s1"}])
    assert report.checked == 0
    assert "nothing to reconcile" in report.summary()


def test_registering_replaces_and_can_be_listed():
    @reconciler("x.handler")
    def first(**kw):
        return Observation()

    assert "x.handler" in registered_reconcilers()


# ---------------------------------------------------------------------------
# 6. CLI
# ---------------------------------------------------------------------------

def _write_wal(path, records):
    import json

    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def test_cli_exit_codes(capsys, tmp_path):
    """Drift and "we could not check" are different problems for different
    people, so they do not share an exit code."""
    from agent_saga.cli import main

    wal = tmp_path / "w.wal"
    _write_wal(wal, [committed(), compensated()])

    # Nothing registered: everything unverifiable.
    assert main(["reconcile", "--wal-path", str(wal)]) == 3
    out = capsys.readouterr().out
    assert "unverifiable" in out and "Register a @reconciler" in out

    @reconciler("stripe.refund")
    def observe(**kw):
        return Observation(reversed_=False, exists=True)

    assert main(["reconcile", "--wal-path", str(wal)]) == 1
    assert "DRIFT" in capsys.readouterr().out

    clear_reconcilers()

    @reconciler("stripe.refund")
    def ok(**kw):
        return Observation(reversed_=True)

    assert main(["reconcile", "--wal-path", str(wal)]) == 0
    assert "reconciled" in capsys.readouterr().out


def test_cli_missing_file(capsys, tmp_path):
    from agent_saga.cli import main

    assert main(["reconcile", "--wal-path", str(tmp_path / "nope.wal")]) == 2
    assert "cannot read" in capsys.readouterr().out


def test_cli_verbose_lists_unverifiable(capsys, tmp_path):
    from agent_saga.cli import main

    wal = tmp_path / "w.wal"
    _write_wal(wal, [committed(), compensated()])
    assert main(["reconcile", "--wal-path", str(wal), "--verbose"]) == 3
    assert "stripe.charge" in capsys.readouterr().out


# -- #27 automatic drift alerting ---------------------------------------------

def _drift_finding():
    from agent_saga.reconcile import Finding, DRIFT
    return Finding("saga-1", "st1", "stripe.charge", "stripe.refund",
                   "REVERSED", DRIFT, detail="charge still live")


_DRIFT_RECORDS = [{
    "event": "STEP_COMMITTED", "saga_id": "saga-1", "step_id": "st1",
    "tool": "stripe.charge", "ts": 1.0, "semantics": "COMPENSABLE",
    "compensation": {"handler": "stripe.refund", "recoverable": True, "kwargs": {}},
}]


async def _identity(v):
    return v


@aio
async def test_reconciliation_fires_on_drift():
    from agent_saga.reconcile import Reconciliation
    alerts = []
    rec = Reconciliation(on_drift=lambda p: alerts.append(p))
    rec._check = lambda effect: _identity(_drift_finding())      # force a DRIFT
    report = await rec.run(_DRIFT_RECORDS)
    assert len(report.drift) == 1 and len(alerts) == 1
    p = alerts[0]
    assert p["type"] == "reconciliation_drift"
    assert p["saga_id"] == "saga-1" and p["tool"] == "stripe.charge"
    assert p["expected"] == "REVERSED" and "summary" in p


@aio
async def test_reconciliation_on_drift_supports_async_callback():
    from agent_saga.reconcile import Reconciliation
    got = []
    async def cb(payload): got.append(payload)
    rec = Reconciliation(on_drift=cb)
    rec._check = lambda effect: _identity(_drift_finding())
    await rec.run(_DRIFT_RECORDS)
    assert len(got) == 1


@aio
async def test_reconciliation_on_drift_exception_does_not_break_sweep():
    from agent_saga.reconcile import Reconciliation
    def boom(payload): raise RuntimeError("pagerduty down")
    rec = Reconciliation(on_drift=boom)
    rec._check = lambda effect: _identity(_drift_finding())
    report = await rec.run(_DRIFT_RECORDS)    # must not raise
    assert len(report.drift) == 1             # drift still recorded


@aio
async def test_reconciliation_no_alert_when_confirmed():
    from agent_saga.reconcile import Reconciliation, Finding, CONFIRMED
    alerts = []
    rec = Reconciliation(on_drift=lambda p: alerts.append(p))
    rec._check = lambda effect: _identity(
        Finding("s", "st", "t", "h", "REVERSED", CONFIRMED))
    await rec.run(_DRIFT_RECORDS)
    assert alerts == []                        # only DRIFT alerts
