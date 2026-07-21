"""Time-travel debugger: WAL parsing, status derivation, scrubbing, and the
HTTP surface. A real saga is run to produce a real WAL, then parsed back."""

import json
import tempfile
import threading
import urllib.request
from pathlib import Path

import pytest

from agent_saga import (
    ActionSemantics,
    AsyncWAL,
    Compensation,
    PreFlightGate,
    SagaContext,
)
from agent_saga.ui.reader import (
    FAILED,
    IN_PROGRESS,
    ROLLED_BACK,
    SUCCESS,
    REDACTED,
    SagaWALReader,
    iter_records,
    scrub,
)
from agent_saga.ui.server import make_server
from conftest import aio

C = ActionSemantics.COMPENSABLE
I = ActionSemantics.IRREVERSIBLE


async def _run_saga(wal_path, *, fail=False, gate=None):
    wal = AsyncWAL(wal_path)
    await wal.start()
    ctx = SagaContext(gate=gate, wal=wal)
    await ctx.begin()
    try:
        await ctx.execute(
            tool="stripe.charge", semantics=C,
            forward=lambda customer, amount: {"id": "ch_1", "amount": amount},
            compensate=lambda r: Compensation(
                fn=lambda **k: None, handler="stripe.refund",
                kwargs={"charge_id": r["id"], "amount": r["amount"]},
                idempotency_key="idem-1"),
            forward_kwargs={"customer": "cus_9", "amount": 4200})
        await ctx.execute(
            tool="crm.update", semantics=C, forward=lambda: {"prev": "lead"},
            compensate=lambda r: Compensation(fn=lambda **k: None, handler="crm.revert",
                                              kwargs={"prev": r["prev"]}))
        if fail:
            raise ValueError("boom")
    except BaseException:
        report = await ctx.rollback()
        await ctx.finish(aborted=True, clean=report.clean)
    else:
        await ctx.finish()
    await wal.close()
    return ctx.saga_id


# ==========================================================================
# Scrubbing
# ==========================================================================

def test_scrub_redacts_credential_shaped_values():
    assert scrub("sk_live_abcdefghij1234567890") == REDACTED
    assert scrub("postgresql://u:pw@host/db") == REDACTED


def test_scrub_redacts_credential_named_keys_but_keeps_refs():
    out = scrub({"auth_token": "whatever", "credential_ref": "stripe_prod", "amount": 42})
    assert out["auth_token"] == REDACTED
    assert out["credential_ref"] == "stripe_prod"
    assert out["amount"] == 42


def test_scrub_recurses_and_truncates():
    out = scrub({"nested": {"api_key": "x"}, "note": "a" * 5000})
    assert out["nested"]["api_key"] == REDACTED
    assert out["note"].endswith("…") and len(out["note"]) < 5000


# ==========================================================================
# Truncation tolerance
# ==========================================================================

def test_iter_records_skips_a_truncated_final_line():
    from agent_saga.ui.reader import ParseStats

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "wal.jsonl"
        p.write_text(
            json.dumps({"seq": 1, "event": "SAGA_START", "saga_id": "s1", "ts": 1.0}) + "\n"
            + '{"seq": 2, "event": "STEP_INT',  # crash mid-write, no newline
            encoding="utf-8")
        stats = ParseStats()
        recs = list(iter_records(p, stats))
        assert len(recs) == 1
        assert stats.corrupt_lines == 1


def test_missing_file_is_not_an_error():
    reader = SagaWALReader(Path("does") / "not" / "exist.wal")
    assert reader.list_sagas() == {"sagas": [], "total": 0, "corrupt_lines": 0}
    assert reader.get_saga("anything") is None
    assert reader.meta()["exists"] is False


# ==========================================================================
# Status derivation
# ==========================================================================

@aio
async def test_successful_saga_reads_as_success():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "wal.jsonl"
        sid = await _run_saga(p)
        detail = SagaWALReader(p).get_saga(sid)
    assert detail["status"] == SUCCESS
    assert detail["step_count"] == 2
    assert all(s["status"] == "COMMITTED" for s in detail["steps"])


@aio
async def test_rolled_back_saga_reads_as_rolled_back_with_compensated_steps():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "wal.jsonl"
        sid = await _run_saga(p, fail=True)
        detail = SagaWALReader(p).get_saga(sid)
    assert detail["status"] == ROLLED_BACK
    assert detail["rollback_clean"] is True
    assert [s["status"] for s in detail["steps"]] == ["COMPENSATED", "COMPENSATED"]


@aio
async def test_failed_saga_with_orphan_reads_as_failed():
    """An approved IRREVERSIBLE step cannot be undone -> rollback not clean."""
    gate = PreFlightGate(approval_provider=lambda ctx, rule: True)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "wal.jsonl"
        wal = AsyncWAL(p)
        await wal.start()
        ctx = SagaContext(gate=gate, wal=wal)
        await ctx.begin()
        try:
            await ctx.execute(tool="email.send", semantics=I, forward=lambda: "sent")
            raise ValueError("boom")
        except BaseException:
            report = await ctx.rollback()
            await ctx.finish(aborted=True, clean=report.clean)
        await wal.close()
        detail = SagaWALReader(p).get_saga(ctx.saga_id)
    assert detail["status"] == FAILED
    assert detail["steps"][0]["status"] == "ORPHANED"


@aio
async def test_crashed_saga_with_no_terminal_reads_as_in_progress():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "wal.jsonl"
        wal = AsyncWAL(p)
        await wal.start()
        ctx = SagaContext(wal=wal)
        await ctx.begin()
        await ctx.execute(tool="t", semantics=C, forward=lambda: 1,
                          compensate=lambda r: Compensation(fn=lambda: None, handler="h"))
        await wal.close()  # no finish() -> looks like a crash
        detail = SagaWALReader(p).get_saga(ctx.saga_id)
    assert detail["status"] == IN_PROGRESS


@aio
async def test_secrets_never_appear_in_the_detail_payload():
    """Even if an agent passed a raw secret as a forward kwarg, the UI must not
    surface it."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "wal.jsonl"
        wal = AsyncWAL(p)
        await wal.start()
        ctx = SagaContext(wal=wal)
        await ctx.begin()
        await ctx.execute(tool="t", semantics=C, forward=lambda **k: 1,
                          forward_kwargs={"api_key": "sk_live_leakleakleak123456"},
                          compensate=lambda r: Compensation(fn=lambda **k: None, handler="h"))
        await ctx.finish()
        await wal.close()
        detail = SagaWALReader(p).get_saga(ctx.saga_id)
    # Secret is ASCII, so it would show in the serialized form if it leaked.
    assert "sk_live_leakleakleak123456" not in json.dumps(detail)
    # Assert on the structure, not the JSON string (json.dumps escapes the
    # non-ASCII redaction marker to \uXXXX).
    assert detail["steps"][0]["forward_kwargs"]["api_key"] == REDACTED


@aio
async def test_abort_cause_surfaces_in_saga_detail():
    """A saga run through the @saga boundary records why it aborted; the reader
    exposes it as abort_cause for the timeline's failure marker."""
    from agent_saga import saga, tool

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "wal.jsonl"
        wal = AsyncWAL(p)
        await wal.start()

        @tool(semantics=C, compensate=lambda r: Compensation(fn=lambda **k: None, handler="h"))
        def act():
            return {"id": 1}

        @saga(wal=wal, reraise=False)
        async def run():
            await act()
            raise RuntimeError("downstream 500 from CRM")

        await run()
        await wal.close()

        reader = SagaWALReader(p)
        sid = reader.list_sagas()["sagas"][0]["saga_id"]
        detail = reader.get_saga(sid)

    assert detail["status"] == ROLLED_BACK
    assert detail["abort_cause"]["type"] == "RuntimeError"
    assert "downstream 500" in detail["abort_cause"]["message"]


@aio
async def test_successful_saga_has_no_abort_cause():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "wal.jsonl"
        sid = await _run_saga(p)
        detail = SagaWALReader(p).get_saga(sid)
    assert detail["abort_cause"] is None


@aio
async def test_abort_cause_message_is_scrubbed_if_it_is_a_bare_secret():
    """A message that is itself a credential must not pass through to the UI."""
    from agent_saga import saga

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "wal.jsonl"
        wal = AsyncWAL(p)
        await wal.start()

        @saga(wal=wal, reraise=False)
        async def run():
            raise RuntimeError("sk_live_abcdefghij1234567890")

        await run()
        await wal.close()
        reader = SagaWALReader(p)
        sid = reader.list_sagas()["sagas"][0]["saga_id"]
        detail = reader.get_saga(sid)

    assert detail["abort_cause"]["message"] == REDACTED


@aio
async def test_list_sorts_newest_first():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "wal.jsonl"
        first = await _run_saga(p)
        second = await _run_saga(p, fail=True)
        listing = SagaWALReader(p).list_sagas()
    assert listing["total"] == 2
    assert listing["sagas"][0]["saga_id"] == second  # most recent first


# ==========================================================================
# HTTP surface
# ==========================================================================

@aio
async def test_http_endpoints_serve_dashboard_and_api():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "wal.jsonl"
        sid = await _run_saga(p, fail=True)

        httpd = make_server(str(p), host="127.0.0.1", port=0)
        # Join handler threads on close so no accepted-connection socket lingers
        # for the GC to complain about (strict filterwarnings=error).
        httpd.daemon_threads = False
        httpd.block_on_close = True
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        base = f"http://127.0.0.1:{port}"

        def get(path):
            with urllib.request.urlopen(base + path) as r:
                return r.read().decode()

        try:
            assert "Time-Travel Debugger" in get("/")

            meta = json.loads(get("/api/meta"))
            assert meta["exists"] is True

            sagas = json.loads(get("/api/sagas"))
            assert sagas["total"] == 1 and sagas["sagas"][0]["saga_id"] == sid

            detail = json.loads(get("/api/sagas/" + sid))
            assert detail["status"] == ROLLED_BACK
            assert len(detail["steps"]) == 2

            # 404 for unknown saga. urlopen raises before a `with` body would
            # run, so the error response's socket must be closed by hand.
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(base + "/api/sagas/nope")
            assert exc.value.code == 404
            exc.value.close()
        finally:
            httpd.shutdown()
            httpd.server_close()


@aio
async def test_http_token_auth_gates_every_route():
    import urllib.error

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "wal.jsonl"
        await _run_saga(p, fail=True)

        httpd = make_server(str(p), host="127.0.0.1", port=0, token="s3cr3t")
        httpd.daemon_threads = False
        httpd.block_on_close = True
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        base = f"http://127.0.0.1:{port}"

        def get(path, headers=None):
            req = urllib.request.Request(base + path, headers=headers or {})
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode()

        try:
            # No token -> 401 on both the dashboard and the API.
            for path in ("/", "/api/sagas"):
                with pytest.raises(urllib.error.HTTPError) as exc:
                    get(path)
                assert exc.value.code == 401
                assert exc.value.headers.get("WWW-Authenticate", "").startswith("Bearer")
                exc.value.close()

            # Wrong token -> 401.
            with pytest.raises(urllib.error.HTTPError) as exc:
                get("/api/sagas", {"Authorization": "Bearer nope"})
            assert exc.value.code == 401
            exc.value.close()

            # Correct token via header -> 200.
            status, body = get("/api/sagas", {"Authorization": "Bearer s3cr3t"})
            assert status == 200 and json.loads(body)["total"] == 1

            # Correct token via query param -> 200 (the browser-open path).
            status, _ = get("/api/meta?token=s3cr3t")
            assert status == 200
        finally:
            httpd.shutdown()
            httpd.server_close()


@aio
async def test_http_no_token_configured_stays_open():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "wal.jsonl"
        await _run_saga(p, fail=False)
        httpd = make_server(str(p), host="127.0.0.1", port=0)   # no token
        httpd.daemon_threads = False
        httpd.block_on_close = True
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/meta") as r:
                assert r.status == 200
        finally:
            httpd.shutdown()
            httpd.server_close()
