"""Tests for agent-saga v0.1.9 core hardening, architectural refinements, and killer features."""

import asyncio
import os
import pytest
from conftest import aio

from agent_saga import (
    SagaEngine,
    SagaConfig,
    KeyRingEncryptor,
    AutoLockHeartbeat,
    IncrementalCompensationTracker,
    streaming_step,
    OTLPExporter,
    SelfHealingPromptFeedback,
)
from agent_saga.wal import FileWAL
from agent_saga.locks import SemanticLockManager
from agent_saga.entanglement import EntanglementMatrix, HEADER_ENTANGLEMENT_ID
from agent_saga.ui import get_saga_ui_app
from agent_saga.gate import Verdict


@aio
async def test_file_wal_non_blocking_read(tmp_path):
    wal_file = tmp_path / "test_async.wal"
    wal = FileWAL(wal_file)
    await wal.start()
    wal.append("TEST_EVENT", {"data": 123})
    await wal.barrier()
    records = await wal.read_all()
    assert len(records) == 1
    assert records[0]["event"] == "TEST_EVENT"
    await wal.close()


@aio
async def test_auto_lock_heartbeat():
    mgr = SemanticLockManager()
    await mgr.acquire("res_1", "saga_1", ttl=1.0)
    async with AutoLockHeartbeat("res_1", "saga_1", manager=mgr, interval=0.1):
        await asyncio.sleep(0.3)
        assert mgr.owner("res_1") == "saga_1"
    mgr.release("res_1", "saga_1")


def test_key_ring_rotation():
    from cryptography.fernet import Fernet
    k1 = Fernet.generate_key()
    k2 = Fernet.generate_key()

    ring = KeyRingEncryptor(primary_key=k1, fallback_keys=[k2])
    token = ring.encrypt(b"secret payload")
    decrypted = ring.decrypt(token)
    assert decrypted == b"secret payload"

    # Old token created with k2 should decrypt via fallback
    old_ring = KeyRingEncryptor(primary_key=k2)
    old_token = old_ring.encrypt(b"old payload")
    decrypted_old = ring.decrypt(old_token)
    assert decrypted_old == b"old payload"


def test_entanglement_pruning_and_headers():
    matrix = EntanglementMatrix(max_nodes=2, ttl_seconds=10.0)
    headers = matrix.inject_headers({}, parent_step="step_1")
    assert HEADER_ENTANGLEMENT_ID in headers
    extracted = EntanglementMatrix.extract_headers(headers)
    assert extracted["matrix_id"] == matrix.matrix_id
    assert extracted["parent_step"] == "step_1"


def test_saga_engine_configure():
    cfg = SagaEngine.configure(telemetry=True)
    assert isinstance(cfg, SagaConfig)
    assert cfg.telemetry is True


@aio
async def test_incremental_streaming_compensation():
    unwound_chunks = []

    def undo_stream(chunks):
        unwound_chunks.extend(chunks)

    @streaming_step("chunk_write", undo_fn=undo_stream)
    async def sample_stream(tracker):
        tracker.record_chunk("part1")
        tracker.record_chunk("part2")
        raise ValueError("stream aborted")

    with pytest.raises(ValueError):
        await sample_stream()

    assert len(unwound_chunks) == 2
    assert unwound_chunks[0].data == "part1"


def test_otlp_exporter():
    exporter = OTLPExporter(endpoint="http://localhost:4318/v1/traces")
    span = exporter.create_span("saga.step.charge", "saga_123", attributes={"amount": 500})
    assert span["name"] == "saga.step.charge"
    assert len(exporter.spans) == 1


def test_get_saga_ui_app():
    app = get_saga_ui_app()
    assert app is not None


def test_self_healing_feedback():
    from agent_saga.gate import Decision, Verdict
    decision = Decision(verdict=Verdict.BLOCK, rule="credit_limit", reason="Insufficient credit limit for Stripe charge")
    feedback = SelfHealingPromptFeedback.from_decision("stripe.charge", decision, hallucination_score=0.95)
    assert feedback is not None
    instruction = feedback.to_system_prompt_instruction()
    assert "SAGA PROTECTION GATE REJECTION" in instruction
    assert "stripe.charge" in instruction
