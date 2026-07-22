import pytest

from agent_saga import (
    InvariantRule,
    MissionCriticalGate,
    MissionCriticalViolation,
    TripleRedundantVerifier,
    patch_all,
)


def test_mission_critical_gate_medical_dosage():
    # Medical safety rule: dosage must be between 0.1 mg and 50.0 mg
    dosage_rule = InvariantRule(
        name="medical_dosage_range",
        check_fn=lambda p: 0.1 <= p.get("dosage_mg", 0) <= 50.0,
        error_message="Dosage exceeds safe medical boundaries (0.1mg - 50mg)",
    )
    gate = MissionCriticalGate([dosage_rule])

    # Valid dosage
    gate.validate_preflight({"dosage_mg": 12.5})

    # Dangerous overdose attempt
    with pytest.raises(MissionCriticalViolation) as exc_info:
        gate.validate_preflight({"dosage_mg": 500.0})
    assert "exceeds safe medical boundaries" in str(exc_info.value)


def test_triple_redundant_verifier_financial_wire():
    # Financial banking wire verification: structural, range, and ledger checks
    verifier = TripleRedundantVerifier(
        structural_checker=lambda p: "account_id" in p and "amount" in p,
        boundary_checker=lambda p: 0 < p.get("amount", 0) <= 1_000_000,
        ledger_checker=lambda p: p.get("account_status") == "ACTIVE",
    )

    # 3/3 Consensus Pass
    ok, msg, results = verifier.verify_consensus({
        "account_id": "ACC_9988",
        "amount": 250_000,
        "account_status": "ACTIVE",
    })
    assert ok
    assert "3/3 Triple Redundant Consensus Verified" in msg
    assert results == {"structural": True, "boundary": True, "ledger": True}

    # Failed Consensus (Account frozen)
    ok_bad, msg_bad, results_bad = verifier.verify_consensus({
        "account_id": "ACC_9988",
        "amount": 250_000,
        "account_status": "FROZEN",
    })
    assert not ok_bad
    assert "Consensus Failed" in msg_bad
    assert results_bad["ledger"] is False


def test_global_auto_patching():
    installed = patch_all()
    assert installed
