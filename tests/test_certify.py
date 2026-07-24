"""Rollback-safety certificates (#flagship-2): prove every committed effect is
accounted for, and fail CI when one is not."""

import json

from conftest import aio
from agent_saga import ActionSemantics, PreFlightGate
from agent_saga.context import Compensation, SagaContext
from agent_saga.certify import certify_rollback_safety, CRITICAL, WARNING
from agent_saga.wal.file_wal import FileWAL


async def _run(path, *, orphan=False):
    wal = FileWAL(path)
    await wal.start()
    gate = PreFlightGate(approval_provider=lambda ctx, rule: True)
    ctx = SagaContext(gate=gate, wal=wal)
    await ctx.begin()
    try:
        await ctx.execute(
            tool="stripe.charge", semantics=ActionSemantics.COMPENSABLE,
            forward=lambda: {"id": "ch_1"},
            compensate=lambda r: Compensation(
                fn=lambda **k: None, handler="stripe.refund",
                kwargs={"charge_id": r["id"]}))
        if orphan:
            await ctx.execute(tool="email.send",
                              semantics=ActionSemantics.IRREVERSIBLE,
                              forward=lambda: "sent")
        raise ValueError("boom")
    except BaseException:
        rep = await ctx.rollback()
        await ctx.finish(aborted=True, clean=rep.clean)
    await wal.close()
    return wal.records()


@aio
async def test_clean_rollback_certifies_safe(tmp_path):
    cert = certify_rollback_safety(await _run(tmp_path / "c.wal"))
    assert cert.safe and not cert.critical
    assert "SAFE" in cert.summary()
    assert len(cert.merkle_root) == 64          # ties the cert to the exact log


@aio
async def test_orphaned_effect_certifies_unsafe(tmp_path):
    cert = certify_rollback_safety(await _run(tmp_path / "o.wal", orphan=True))
    assert not cert.safe
    # it names the exact step that could not be undone
    assert any("orphaned effect" in f.issue and f.tool == "email.send"
               for f in cert.critical)
    assert any("did not complete cleanly" in f.issue for f in cert.critical)


def test_dangling_saga_is_a_warning_not_a_failure():
    records = [{"seq": 1, "saga_id": "s1", "event": "SAGA_START", "ts": 1.0}]
    cert = certify_rollback_safety(records)
    assert cert.safe                             # unproven, not proven-harmful
    assert any(f.severity == WARNING and "unrecovered" in f.issue for f in cert.findings)


def test_unregistered_handler_warns_when_requested(tmp_path):
    import asyncio
    records = asyncio.run(_run(tmp_path / "h.wal"))
    cert = certify_rollback_safety(records, require_registered_handlers=True)
    # stripe.refund is not registered in this process -> recovery would escalate
    assert any("not registered here" in f.issue for f in cert.warnings)
    assert cert.safe                             # a warning, not a critical


def test_certificate_serialises():
    cert = certify_rollback_safety([{"seq": 1, "saga_id": "s", "event": "SAGA_START"}])
    d = cert.to_dict()
    json.dumps(d)                                # must be JSON-safe
    assert d["merkle_root"] and "findings" in d and d["version"] >= 1


def test_cli_certify_gates_ci(tmp_path, capsys):
    from agent_saga.cli import main
    unsafe = tmp_path / "u.wal"
    unsafe.write_text("\n".join(json.dumps(r) for r in [
        {"seq": 1, "saga_id": "p", "event": "SAGA_START", "ts": 1.0},
        {"seq": 2, "saga_id": "p", "event": "SAGA_ABORTED", "ts": 2.0},
        {"seq": 3, "saga_id": "p", "event": "ROLLBACK_START", "ts": 3.0},
        {"seq": 4, "saga_id": "p", "event": "ROLLBACK_END", "clean": False, "ts": 4.0},
    ]) + "\n", encoding="utf-8")
    assert main(["certify", "--wal", str(unsafe)]) == 1      # non-zero -> CI fails
    assert "UNSAFE" in capsys.readouterr().out

    safe = tmp_path / "s.wal"
    safe.write_text(json.dumps(
        {"seq": 1, "saga_id": "q", "event": "SAGA_COMPLETE", "ts": 1.0}) + "\n",
        encoding="utf-8")
    assert main(["certify", "--wal", str(safe)]) == 0
