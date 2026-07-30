import pytest
from agent_saga._integrity_guard import (
    SAGAOPS_ENGINE_SIGNATURE,
    generate_wal_provenance_token,
    verify_wal_provenance_token,
    verify_engine_integrity,
)


def test_wal_provenance_token_generation_and_verification():
    saga_id = "tx_998811"
    tool_name = "stripe.charge"

    token = generate_wal_provenance_token(saga_id, tool_name)
    assert len(token) == 64  # SHA-256 hex string

    valid = verify_wal_provenance_token(saga_id, tool_name, token)
    assert valid is True

    # Tampered payload fails verification
    invalid = verify_wal_provenance_token(saga_id, "stripe.refund", token)
    assert invalid is False


def test_engine_integrity_verification():
    res = verify_engine_integrity()
    assert res["status"] == "VERIFIED"
    assert res["engine"] == SAGAOPS_ENGINE_SIGNATURE
    assert len(res["master_fingerprint"]) == 64
