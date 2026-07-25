import pytest
from pathlib import Path

from agent_saga import (
    BeastEngine,
    BeastExecutionSummary,
    InvariantRule,
)
from conftest import aio


@aio
async def test_beast_engine_full_lifecycle(tmp_path):
    beast = BeastEngine(name="test_beast_node", memory_dir=tmp_path / "memory")

    # Register self-healing path
    def primary_payment(**kwargs):
        raise RuntimeError("Payment Gateway A unreachable")

    def fallback_payment(**kwargs):
        return {"tx_id": "tx_fallback_999", "status": "COMPLETED"}

    beast.register_healing_rule("pay_gateway_a", "pay_gateway_b", fallback_payment)

    # Mission-critical invariant rule
    rules = [
        InvariantRule(
            name="amount_limit",
            check_fn=lambda p: p.get("amount", 0) <= 50000,
            error_message="Amount exceeds maximum threshold",
        )
    ]

    tool_sequence = [
        {
            "name": "pay_gateway_a",
            "args": {"amount": 2500},
            "fn": primary_payment,
        }
    ]

    preview_checks = [lambda: {"wallet_status": "active", "balance": 10000}]

    summary = await beast.run_beast_saga(
        saga_id="beast_tx_101",
        tool_sequence=tool_sequence,
        preview_checks=preview_checks,
        invariant_rules=rules,
    )

    assert isinstance(summary, BeastExecutionSummary)
    assert summary.status == "HEALED"
    assert "pay_gateway_a (healed -> pay_gateway_b)" in summary.executed_tools
    assert summary.preview_plan["preflight_results"]["beast_check_1"]["balance"] == 10000
    assert summary.audit_chain_intact is True
    assert summary.execution_time_ms > 0
