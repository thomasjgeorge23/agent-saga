import asyncio
from saga_service.service import app, check_preflight_gate, get_service_status, run_manual_gc_sweep, GateCheckRequest


def test_saga_service_status_endpoint():
    data = asyncio.run(get_service_status())
    assert data["service"] == "SAGAOPS Control Plane"
    assert data["byok_encryption"]["status"] == "ACTIVE"
    assert data["gate"]["rules_count"] >= 2
    assert data["gate"]["high_value_escalation_limit"] == 5000.0


def test_preflight_gate_check_endpoint_allow():
    req = GateCheckRequest(tool="stripe.charge", kwargs={"amount": 250, "currency": "USD"})
    data = asyncio.run(check_preflight_gate(req))
    assert data["verdict"] == "ALLOW"


def test_preflight_gate_check_endpoint_high_value_escalation():
    req = GateCheckRequest(tool="stripe.charge", kwargs={"amount": 9500, "currency": "USD"})
    data = asyncio.run(check_preflight_gate(req))
    assert data["rule"] == "high_value_escalation"


def test_preflight_gate_check_endpoint_anti_spam_block():
    req = GateCheckRequest(tool="db.query", kwargs={"query": "DROP_TABLE production_users"})
    data = asyncio.run(check_preflight_gate(req))
    assert data["verdict"] == "BLOCK"
    assert data["rule"] == "anti_spam_keyword_filter"


def test_manual_gc_sweep_endpoint():
    data = asyncio.run(run_manual_gc_sweep())
    assert data["status"] == "OK"
    assert "metrics" in data
