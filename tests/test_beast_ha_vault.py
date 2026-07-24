import asyncio
import tempfile
import pytest
from pathlib import Path

from agent_saga import (
    LeaderElection,
    NodeState,
    SagaDiagnosticSuite,
    WALReplicator,
    WORMVault,
    VaultTamperError,
    ComplianceEngine,
    AsyncWAL,
)
from conftest import aio


@aio
async def test_leader_election_lifecycle():
    election = LeaderElection(node_id="node_primary")
    assert election.state == NodeState.STANDBY

    await election.start()
    assert election.is_leader()
    assert election.state == NodeState.LEADER

    await election.stop()
    assert not election.is_leader()


@aio
async def test_wal_replicator():
    wal = AsyncWAL()
    await wal.start()

    replicator = WALReplicator(primary_wal=wal, target_endpoints=["http://standby-1:8080"])
    await replicator.start()

    count = await replicator.replicate_batch([{"event": "STEP1"}, {"event": "STEP2"}])
    assert count == 2
    assert replicator.replicated_count == 2

    await replicator.stop()
    await wal.close()


@aio
async def test_saga_diagnostic_suite(tmp_path):
    wal_path = tmp_path / "wal.jsonl"
    wal = AsyncWAL(wal_path)
    await wal.start()

    wal.append("SAGA_BEGIN", {"saga_id": "s100"})
    wal.append("STEP_INTENT", {"saga_id": "s100", "tool_name": "pay"})
    wal.append("SAGA_FINISH", {"saga_id": "s100"})
    await wal.barrier()

    suite = SagaDiagnosticSuite(wal_instance=wal)
    res = await suite.run_full_diagnostics()
    assert res["status"] == "PASS"
    assert res["total_records"] == 3
    assert res["dangling_sagas"] == 0

    await wal.close()


def test_worm_vault_and_tamper_detection(tmp_path):
    vault_file = tmp_path / "audit_vault.jsonl"
    secret_key = b"super_secret_audit_key_32_bytes!"

    vault = WORMVault(vault_file, secret_key)
    vault.write_entry(saga_id="saga_99", event_type="PAYMENT", payload={"amount": 500, "user": "alice"})

    entries = vault.verify_vault()
    assert len(entries) == 1
    assert entries[0]["payload"]["amount"] == 500

    # Simulate tampered data injection
    with open(vault_file, "r+", encoding="utf-8") as f:
        content = f.read()
        f.seek(0)
        f.write(content.replace("500", "999999"))

    with pytest.raises(VaultTamperError, match="TAMPER DETECTED"):
        vault.verify_vault()


def test_compliance_engine_pii_sanitization():
    raw_payload = {
        "user_id": "u123",
        "email": "alice@example.com",
        "cvv": "123",
        "details": {"phone": "+1-555-0199", "item": "laptop"},
    }
    sanitized = ComplianceEngine.sanitize_payload(raw_payload)

    assert sanitized["user_id"] == "u123"
    assert sanitized["email"] == "[REDACTED_PII]"
    assert sanitized["cvv"] == "[REDACTED_PII]"
    assert sanitized["details"]["phone"] == "[REDACTED_PII]"
    assert sanitized["details"]["item"] == "laptop"
