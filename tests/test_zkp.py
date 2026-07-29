"""Blind audit commitments: a proof an auditor can actually check.

Deeper adversarial coverage lives in `test_v050_honesty.py`, which pins the
three defects this module shipped with in 0.5.0 (a hardcoded key, a Merkle
path that held the root, and a verification that accepted forgeries).
"""

import pytest

from agent_saga.zkp import (
    AuditCommitmentError,
    BlindAuditCommitments,
    ZeroKnowledgeAuditProof,
)

KEY = b"an-audit-key-of-adequate-length!"


def test_commitment_generation_and_verification():
    engine = BlindAuditCommitments(KEY)

    records = [
        {"saga_id": "tx_101", "event": "PAYMENT_DEDUCTED",
         "payload": {"amount": 500, "user": "alice@secret.com"}},
        {"saga_id": "tx_101", "event": "ORDER_CREATED",
         "payload": {"item": "laptop"}},
    ]

    root, commitments = engine.build(records)

    assert len(commitments) == 2
    assert len(root) == 64
    assert commitments[0].commitment_hash != commitments[1].commitment_hash

    # An auditor holding only the root and the proof can confirm inclusion
    # without ever seeing the payload.
    for commitment in commitments:
        assert engine.verify(commitment, root) is True

    # And the payload really is absent from what they were handed.
    for commitment in commitments:
        assert "alice@secret.com" not in str(commitment.to_dict())


def test_the_key_is_required():
    """Up to 0.5.0 this fell back to a constant compiled into the published
    package, which made every commitment recoverable by anyone who installed
    it."""
    with pytest.raises(AuditCommitmentError, match="required"):
        ZeroKnowledgeAuditProof()


def test_merkle_path_no_longer_pretends_the_root_is_a_path():
    engine = BlindAuditCommitments(KEY)
    root, commitments = engine.build(
        [{"saga_id": f"tx{i}", "event": "E", "payload": {"i": i}} for i in range(4)])
    assert root not in commitments[0].merkle_path
    assert len(commitments[0].merkle_path) == 2        # depth of a 4-leaf tree
