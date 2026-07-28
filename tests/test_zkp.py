import pytest
from agent_saga.zkp import ZeroKnowledgeAuditProof, ZKProofCommitment


def test_zkp_commitment_generation_and_verification():
    zkp = ZeroKnowledgeAuditProof(audit_salt=b"test_salt_12345")

    records = [
        {"saga_id": "tx_101", "event": "PAYMENT_DEDUCTED", "payload": {"amount": 500, "user": "alice@secret.com"}},
        {"saga_id": "tx_101", "event": "ORDER_CREATED", "payload": {"item": "laptop"}},
    ]

    merkle_root, commitments = zkp.generate_proof_tree(records)

    assert len(commitments) == 2
    assert len(merkle_root) == 64
    assert commitments[0].commitment_hash != commitments[1].commitment_hash

    # Verify commitment against root without revealing payload
    valid = zkp.verify_commitment_against_root(commitments[0], merkle_root)
    assert valid is True
