"""WAL-at-rest encryption (BYOK)."""

import json
import tempfile
from pathlib import Path

import pytest

from agent_saga import (
    ActionSemantics,
    AsyncWAL,
    Compensation,
    RecoveryDaemon,
    SagaContext,
    set_wal_encryptor,
)
from conftest import aio

pytest.importorskip("cryptography")

from agent_saga import FernetEncryptor, generate_key  # noqa: E402
from agent_saga.encryption import EncryptedRecordError, decode_line, encode_line  # noqa: E402

C = ActionSemantics.COMPENSABLE


def _enc():
    return FernetEncryptor(generate_key())


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------

def test_encode_decode_round_trip():
    enc = _enc()
    rec = {"seq": 1, "event": "STEP_INTENT", "saga_id": "s1", "kwargs": {"amount": 42}}
    line = encode_line(rec, enc)
    assert line.startswith("E1:")
    assert decode_line(line, enc) == rec


def test_plaintext_line_is_unprefixed_and_readable_without_a_key():
    rec = {"seq": 1, "saga_id": "s1"}
    line = encode_line(rec, None)
    assert not line.startswith("E1:")
    assert decode_line(line, None) == rec


# --------------------------------------------------------------------------
# On-disk WAL is actually ciphertext
# --------------------------------------------------------------------------

@aio
async def test_wal_on_disk_is_encrypted_and_holds_no_plaintext_payload():
    enc = _enc()
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "wal.jsonl"
        wal = AsyncWAL(path, encryptor=enc)
        await wal.start()
        ctx = SagaContext(wal=wal)
        marker = "TOPSECRET_ROW_VALUE_9988"
        await ctx.execute(tool="crm.update", semantics=C,
                          forward=lambda: {"note": marker},
                          compensate=lambda r: Compensation(fn=lambda: None, handler="h"))
        await wal.close()

        raw = path.read_text(encoding="utf-8")
        assert marker not in raw                 # payload is not on disk in the clear
        assert all(l.startswith("E1:") for l in raw.splitlines() if l.strip())

        # A WAL opened with the same key reads it straight back.
        reader = AsyncWAL(path, encryptor=enc)
        await reader.start()
        events = [r["event"] for r in reader.records()]
        await reader.close()
        assert "STEP_COMMITTED" in events


# --------------------------------------------------------------------------
# Fail loud without the key -- the safety property
# --------------------------------------------------------------------------

def test_decoding_an_encrypted_line_without_a_key_raises():
    line = encode_line({"seq": 1, "saga_id": "s1"}, _enc())
    with pytest.raises(EncryptedRecordError):
        decode_line(line, None)


@aio
async def test_recovery_refuses_an_encrypted_wal_without_the_key():
    """A daemon that read an encrypted WAL as empty would silently abandon every
    crashed saga in it. It must refuse instead."""
    enc = _enc()
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "wal.jsonl"
        wal = AsyncWAL(path, encryptor=enc)
        await wal.start()
        ctx = SagaContext(wal=wal, lease_ttl=0.1)
        await ctx.begin()
        await ctx.execute(tool="t", semantics=C, forward=lambda: "x",
                          compensate=lambda r: Compensation(fn=lambda: None, handler="h"))
        await wal.close()

        # Daemon with NO key configured.
        set_wal_encryptor(None)
        try:
            with pytest.raises(RuntimeError, match="encrypted"):
                RecoveryDaemon(path).scan()
        finally:
            set_wal_encryptor(None)


@aio
async def test_recovery_reads_an_encrypted_wal_with_the_key():
    import time

    enc = _enc()
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "wal.jsonl"
        wal = AsyncWAL(path, encryptor=enc)
        await wal.start()
        ctx = SagaContext(wal=wal, lease_ttl=0.1)
        await ctx.begin()
        await ctx.execute(tool="t", semantics=C, forward=lambda: "x",
                          compensate=lambda r: Compensation(fn=lambda: None, handler="h"))
        # no finish() -> dangles
        await wal.close()

        set_wal_encryptor(enc)
        try:
            time.sleep(0.3)
            sagas = RecoveryDaemon(path).scan()   # must not raise
            assert len(sagas) == 1
        finally:
            set_wal_encryptor(None)


# --------------------------------------------------------------------------
# Env-var resolution and the truncation interaction
# --------------------------------------------------------------------------

@aio
async def test_key_resolves_from_the_environment():
    import os

    key = generate_key()
    os.environ["AGENT_SAGA_WAL_KEY"] = key
    try:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "wal.jsonl"
            wal = AsyncWAL(path)          # no explicit encryptor -> env resolves it
            await wal.start()
            ctx = SagaContext(wal=wal)
            await ctx.execute(tool="t", semantics=C, forward=lambda: "x",
                              compensate=lambda r: Compensation(fn=lambda: None, handler="h"))
            await wal.close()
            raw = path.read_text(encoding="utf-8")
            assert all(l.startswith("E1:") for l in raw.splitlines() if l.strip())
    finally:
        del os.environ["AGENT_SAGA_WAL_KEY"]


@aio
async def test_truncated_encrypted_line_is_skipped_not_fatal():
    enc = _enc()
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "wal.jsonl"
        wal = AsyncWAL(path, encryptor=enc)
        await wal.start()
        ctx = SagaContext(wal=wal)
        await ctx.execute(tool="t", semantics=C, forward=lambda: "x",
                          compensate=lambda r: Compensation(fn=lambda: None, handler="h"))
        await wal.close()

        with open(path, "a", encoding="utf-8") as fh:
            fh.write("E1:dGhpcy1pcy1ub3QtYS12YWxpZC10b2tlbg\n")  # garbage token

        reader = AsyncWAL(path, encryptor=enc)
        await reader.start()
        recs = reader.records()   # bad token skipped, good records returned
        await reader.close()
        assert any(r["event"] == "STEP_COMMITTED" for r in recs)
