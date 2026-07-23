"""Tests for agent-saga v0.2.1 SMALL quick win features (Items 1-10)."""

import os
import re
import pytest
from pathlib import Path
from conftest import aio

from agent_saga import SagaEngine, SagaConfig, SagaConfigError, saga_scope
from agent_saga.connectors._secrets import assert_no_secrets, SecretLeak
from agent_saga.wal.file_wal import FileWAL
from agent_saga.integrity import redact_where
from agent_saga.recovery import RecoveryDaemon
from agent_saga.cli import _format_local_time, build_parser

import json


_SAMPLE_WAL = (
    '{"seq": 1, "saga_id": "s1", "event": "SAGA_START", "pid": 100}\n'
    '{"seq": 2, "saga_id": "s1", "event": "STEP_COMMITTED", "step_id": "st1", '
    '"tool": "stripe.charge", "kwargs": {"amount": 100}}\n'
)


def _run_cli(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def test_cli_version_flag(capsys):
    import agent_saga
    from agent_saga._version import __version__

    parser = build_parser()
    assert parser.prog == "agent-saga"

    # --version exits 0 and prints the single-source-of-truth version.
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert f"agent-saga {__version__}" in out
    # Runtime attribute and CLI must never disagree.
    assert agent_saga.__version__ == __version__


def test_local_timezone_formatting():
    import time
    ts_str = _format_local_time(time.time())
    assert len(ts_str) > 10


def test_approval_timestamps_show_utc_and_local():
    import time
    from agent_saga.cli import _format_timestamps, _format_utc_time
    ts = time.time()
    assert "UTC" in _format_utc_time(ts)
    combined = _format_timestamps(ts)
    # Operators see both zones on one line, no mental conversion required.
    assert "UTC" in combined and "local" in combined


def test_custom_secret_regex():
    kwargs = {"custom_api_token": "INTERNAL_KEY_99999"}
    custom_patterns = [(re.compile(r"^INTERNAL_KEY_\d+"), "Internal Team API Key")]

    with pytest.raises(SecretLeak) as exc:
        assert_no_secrets(kwargs, where="test", custom_patterns=custom_patterns)
    assert "Internal Team API Key" in str(exc.value)


def test_assert_no_secrets_extra_patterns_string_form():
    # The spec's quick form: a plain list of regex strings.
    with pytest.raises(SecretLeak):
        assert_no_secrets({"key": "sk-proj-ABC123xyz"}, where="test",
                          extra_patterns=[r"sk-proj-[A-Za-z0-9]+"])
    # Clean input with the same rule does not trip.
    assert_no_secrets({"note": "hello"}, where="test",
                      extra_patterns=[r"sk-proj-[A-Za-z0-9]+"])


@aio
async def test_recovery_daemon_dry_run(tmp_path):
    wal_file = tmp_path / "test.wal"
    wal_file.write_text('{"seq": 1, "saga_id": "s1", "event": "SAGA_START"}\n', encoding="utf-8")
    daemon = RecoveryDaemon(wal_file, dry_run=True)
    assert daemon.dry_run is True


def test_eager_sagaconfig_validation():
    invalid_enc = "not_an_encryptor_object"
    with pytest.raises(SagaConfigError):
        SagaConfig(encryption=invalid_enc).apply()


def test_sagaconfig_encryption_roundtrip_probe():
    # An encryptor whose decrypt(encrypt(x)) != x is caught at startup, not when
    # the first WAL record later becomes unreadable.
    class Broken:
        def encrypt(self, b): return b"garbage"
        def decrypt(self, b): return b"different"
    with pytest.raises(SagaConfigError):
        SagaConfig(encryption=Broken()).validate()


def test_sagaconfig_connectivity_probe_async_and_sync():
    class AsyncUnreachable:
        def acquire(self): ...
        def release(self): ...
        async def health_check(self):
            raise ConnectionError("refused")

    with pytest.raises(SagaConfigError):
        SagaConfig(semantic_locks=AsyncUnreachable()).validate()

    class SyncDown:
        def ping(self):
            raise OSError("host down")
    with pytest.raises(SagaConfigError):
        SagaConfig(limits=SyncDown()).validate()


def test_sagaconfig_skip_connectivity():
    class AsyncUnreachable:
        def acquire(self): ...
        def release(self): ...
        async def health_check(self):
            raise ConnectionError("refused")
    # Explicitly opting out must not probe the network.
    SagaConfig(semantic_locks=AsyncUnreachable()).validate(check_connectivity=False)


@aio
async def test_sagaconfig_validate_inside_running_loop():
    # Probing an async health check from inside a running loop must not deadlock.
    class AsyncReachable:
        def acquire(self): ...
        def release(self): ...
        async def health_check(self):
            return True
    SagaConfig(semantic_locks=AsyncReachable()).validate()


@aio
async def test_file_wal_rotation(tmp_path):
    wal_file = tmp_path / "rotating.wal"
    wal = FileWAL(wal_file, max_size_mb=0.00001) # tiny threshold
    await wal.start()
    wal.append("SAGA_START", {"saga_id": "s1", "data": "x" * 1000})
    await wal.barrier()
    wal.append("SAGA_START", {"saga_id": "s2", "data": "y" * 1000})
    await wal.barrier()
    await wal.close()
    # Rotation actually happened: at least one timestamped segment on disk.
    rotated = list(tmp_path.glob("rotating_*.wal"))
    assert rotated, "expected a rotated segment file"


@aio
async def test_file_wal_archive_compress_and_retention(tmp_path):
    from agent_saga.wal.file_wal import FileWAL
    archive = tmp_path / "archive"
    wal = FileWAL(tmp_path / "wal.jsonl", max_size_mb=0.00005,
                  archive_dir=archive, compress_archives=True, max_segments=2)
    await wal.start()
    for i in range(6):
        wal.append("SAGA_START", {"saga_id": f"s{i}", "data": "x" * 1000})
        await wal.barrier()
    await wal.close()

    gz = sorted(archive.glob("*.gz"))
    assert len(gz) == 2, "retention must keep only the newest max_segments archives"
    # Segments are compressed and live in the archive dir, not beside the WAL.
    assert not list(tmp_path.glob("wal_*.jsonl"))
    # A compressed archive is still readable/verifiable through the CLI reader.
    from agent_saga.cli import _read_wal
    assert _read_wal(str(gz[0]))


@aio
async def test_file_wal_segment_names_unique_under_rapid_rotation(tmp_path):
    from agent_saga.wal.file_wal import FileWAL
    wal = FileWAL(tmp_path / "w.jsonl", max_size_mb=0.00005)
    await wal.start()
    for i in range(5):
        wal.append("SAGA_START", {"saga_id": f"s{i}", "data": "y" * 1000})
        await wal.barrier()
    await wal.close()
    segs = list(tmp_path.glob("w_*.jsonl"))
    # No sub-second collision clobbering: every rotation kept its own file.
    assert len(segs) == len(set(p.name for p in segs)) >= 3


@aio
async def test_file_wal_rotate_daily(tmp_path):
    wal_file = tmp_path / "daily.wal"
    wal = FileWAL(wal_file, rotate_daily=True)
    await wal.start()
    wal.append("SAGA_START", {"saga_id": "s1"})
    await wal.barrier()
    # Pretend the active segment belongs to a previous day, then write again.
    wal._segment_day = "19990101"
    wal.append("SAGA_START", {"saga_id": "s2"})
    await wal.barrier()
    await wal.close()
    rotated = list(tmp_path.glob("daily_*.wal"))
    assert rotated, "rotate_daily should have rolled the stale segment"


def test_cli_wal_alias_resolves_to_wal_path():
    # --wal is an accepted alias of --wal-path on every subcommand; dest stays
    # wal_path so command handlers are unchanged.
    parser = build_parser()
    a = parser.parse_args(["verify", "--wal", "x.jsonl"])
    assert a.wal_path == "x.jsonl"
    b = parser.parse_args(["verify", "--wal-path", "y.jsonl"])
    assert b.wal_path == "y.jsonl"


def test_cli_recover_runs(tmp_path, capsys):
    # Regression: `recover` used asyncio without importing it -> NameError.
    wal_file = tmp_path / "test.wal"
    wal_file.write_text(_SAMPLE_WAL, encoding="utf-8")
    rc = _run_cli(["recover", "--wal-path", str(wal_file), "--dry-run"])
    assert rc == 0
    assert "Recovery Sweep Completed" in capsys.readouterr().out


def test_cli_export_json_stdout(tmp_path, capsys):
    wal_file = tmp_path / "test.wal"
    wal_file.write_text(_SAMPLE_WAL, encoding="utf-8")
    rc = _run_cli(["export", "--wal-path", str(wal_file), "--format", "json"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert len(parsed) == 2
    assert parsed[0]["saga_id"] == "s1"


def test_cli_export_csv_to_file(tmp_path):
    wal_file = tmp_path / "test.wal"
    wal_file.write_text(_SAMPLE_WAL, encoding="utf-8")
    out_file = tmp_path / "out.csv"
    rc = _run_cli(["export", "--wal-path", str(wal_file), "--format", "csv",
                   "--output", str(out_file)])
    assert rc == 0
    lines = out_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3  # header + 2 rows
    # Nested kwargs survive as compact JSON (CSV-quote-escaped), not a dict repr.
    assert '"{""amount"":100}"' in out_file.read_text(encoding="utf-8")


def test_cli_export_worm_bundle(tmp_path):
    wal_file = tmp_path / "test.wal"
    wal_file.write_text(_SAMPLE_WAL, encoding="utf-8")
    bundle_dir = tmp_path / "worm"
    rc = _run_cli(["export", "--wal-path", str(wal_file), "--out", str(bundle_dir),
                   "--allow-broken"])
    assert rc in (0, 1)  # 1 only if the (unchained) sample is flagged
    assert (bundle_dir / "records.jsonl").exists()


def test_secret_scanner_catches_embedded_custom_credential():
    # An UNANCHORED custom pattern catches a house credential buried mid-string
    # (e.g. copied into a note field). This is what .search buys over .match.
    kwargs = {"note": "the internal key is INTERNAL_KEY_4242 please rotate"}
    custom = [(re.compile(r"INTERNAL_KEY_\d+"), "Internal Team API Key")]
    with pytest.raises(SecretLeak):
        assert_no_secrets(kwargs, where="test", custom_patterns=custom)

    # A built-in pattern stays anchored: a Stripe key is only a leak when it is
    # the whole value, so ordinary prose mentioning it is not a false positive.
    kwargs2 = {"desc": "we rotated the sk_live_ prefix keys last week"}
    assert_no_secrets(kwargs2, where="test")  # must not raise


def test_redact_where_dotted_path_masks_only_the_nested_field():
    from agent_saga.integrity import REDACTED_VALUE
    # Pre-WAL scrub: strip a nested credential, keep the rest of the record.
    records = [
        {"seq": 1, "event": "STEP_INTENT",
         "kwargs": {"card": {"cvv": "123", "last4": "4242"}, "amount": 100}},
        {"seq": 2, "event": "STEP_INTENT", "kwargs": {"other": "value"}},
    ]
    redacted, count = redact_where(records, "kwargs.card.cvv")
    assert count == 1
    assert redacted[0]["kwargs"]["card"]["cvv"] == REDACTED_VALUE
    assert redacted[0]["kwargs"]["card"]["last4"] == "4242"   # rest preserved
    assert redacted[0]["kwargs"]["amount"] == 100
    assert redacted[1] == records[1]                          # no path -> untouched
    assert records[0]["kwargs"]["card"]["cvv"] == "123"       # original not mutated


def test_redact_where_callable_still_whole_record():
    # Callable predicate keeps the GDPR whole-record erasure behaviour.
    from agent_saga.integrity import REDACTED_FIELD
    records = [{"seq": 1, "event": "STEP_COMMITTED", "_h": "h1", "_cd": "c1",
                "kwargs": {"pii": "x"}}]
    redacted, count = redact_where(records, lambda r: r["seq"] == 1)
    assert count == 1
    assert REDACTED_FIELD in redacted[0]
    assert "kwargs" not in redacted[0]


@aio
async def test_human_readable_saga_slug():
    async with saga_scope(name="onboard-acme-123") as ctx:
        assert ctx.name == "onboard-acme-123"
        assert ctx.saga_id.startswith("onboard-acme-123-")


@aio
async def test_saga_name_propagates_to_wal_and_contextvar(tmp_path):
    from agent_saga.wal.file_wal import FileWAL
    from agent_saga.observability import current_saga_name

    wal = FileWAL(tmp_path / "named.wal")
    await wal.start()
    async with saga_scope(name="onboard-acme-123", wal=wal):
        assert current_saga_name() == "onboard-acme-123"
    await wal.close()
    # WAL log carries the readable label alongside the UUID.
    start = [r for r in wal.records() if r.get("event") == "SAGA_START"][0]
    assert start["name"] == "onboard-acme-123"
    # Label is unbound once the scope exits.
    assert current_saga_name() is None


def test_saga_name_shows_in_approval_summary_and_reader():
    from agent_saga.approvals import ApprovalRequest
    req = ApprovalRequest(
        id="abc123def456", saga_id="onboard-acme-123-aeb9", step_id="s1",
        tool="stripe.charge", rule="high_risk", reason="high amount",
        saga_name="onboard-acme-123")
    assert "onboard-acme-123" in req.summary()

    # Reader surfaces name in the dashboard list payload.
    from agent_saga.ui.reader import _SagaAcc
    acc = _SagaAcc(saga_id="onboard-acme-123-aeb9")
    acc.apply({"event": "SAGA_START", "saga_id": "onboard-acme-123-aeb9",
               "name": "onboard-acme-123", "ts": 1.0, "pid": 1})
    assert acc.summary()["name"] == "onboard-acme-123"
