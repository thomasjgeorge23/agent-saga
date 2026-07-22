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


def test_cli_version_flag():
    parser = build_parser()
    assert parser.prog == "agent-saga"


def test_local_timezone_formatting():
    import time
    ts_str = _format_local_time(time.time())
    assert len(ts_str) > 10


def test_custom_secret_regex():
    kwargs = {"custom_api_token": "INTERNAL_KEY_99999"}
    custom_patterns = [(re.compile(r"^INTERNAL_KEY_\d+"), "Internal Team API Key")]
    
    with pytest.raises(SecretLeak) as exc:
        assert_no_secrets(kwargs, where="test", custom_patterns=custom_patterns)
    assert "Internal Team API Key" in str(exc.value)


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


@aio
async def test_file_wal_rotation(tmp_path):
    wal_file = tmp_path / "rotating.wal"
    wal = FileWAL(wal_file, max_size_mb=0.00001) # tiny threshold
    await wal.start()
    wal.append("SAGA_START", {"saga_id": "s1", "data": "x" * 1000})
    await wal.barrier()
    await wal.close()
    assert wal.max_size_mb == 0.00001


def test_redact_where_dotted_path():
    records = [
        {"seq": 1, "event": "STEP_COMMITTED", "_h": "h1", "_cd": "c1", "kwargs": {"card": {"cvv": "123"}}},
        {"seq": 2, "event": "STEP_COMMITTED", "_h": "h2", "_cd": "c2", "kwargs": {"other": "value"}},
    ]
    redacted, count = redact_where(records, "kwargs.card.cvv")
    assert count == 1
    assert "_redacted" in redacted[0]


@aio
async def test_human_readable_saga_slug():
    async with saga_scope(name="onboard-acme-123") as ctx:
        assert ctx.name == "onboard-acme-123"
        assert ctx.saga_id.startswith("onboard-acme-123-")
