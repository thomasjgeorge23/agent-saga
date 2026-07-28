import pytest
from agent_saga.cost_gate import CostBudgetGate, CostBudgetConfig, CostBudgetExceeded


def test_cost_budget_gate_tracking_and_limits():
    gate = CostBudgetGate(CostBudgetConfig(max_cost_usd=1.00, max_total_tokens=1000))

    # 1. Normal usage
    res = gate.record_usage(prompt_tokens=100, completion_tokens=100, cost_usd=0.10)
    assert res["status"] == "OK"
    assert res["requires_operator_approval"] is False

    # 2. Warning threshold (80%)
    res_warn = gate.record_usage(prompt_tokens=300, completion_tokens=300, cost_usd=0.71)
    assert res_warn["requires_operator_approval"] is True

    # 3. Exceeded limit
    with pytest.raises(CostBudgetExceeded):
        gate.record_usage(prompt_tokens=500, completion_tokens=500, cost_usd=0.50)
