import pytest
from agent_saga import (
    EnhancedAIResponse,
    UniversalAgentEngine,
    enhance,
    patch_all,
)
from conftest import aio


@aio
async def test_universal_agent_engine_enhancement():
    engine = UniversalAgentEngine(name="test_universal_engine")

    # Register self-healing path
    def primary_tool(**kwargs):
        raise RuntimeError("Primary database node down")

    def fallback_tool(**kwargs):
        return {"status": "success", "node": "backup_db_replica"}

    engine.register_healing_path("query_db", "query_db_backup", fallback_tool)

    # Simulate raw LLM response (e.g. from OpenAI, Claude, or Gemini)
    raw_llm_text = "I have queried the database and retrieved your account details."
    tool_calls = [
        {"name": "query_db", "args": {"account_id": "acc_771"}, "fn": primary_tool}
    ]
    preview_checks = [lambda: {"wallet_balance": 1500, "status": "verified"}]

    enhanced_res = await engine.execute_enhanced(
        llm_response_text=raw_llm_text,
        tool_calls=tool_calls,
        preview_checks=preview_checks,
    )

    assert isinstance(enhanced_res, EnhancedAIResponse)
    assert enhanced_res.status == "HEALED"
    assert "query_db (healed -> query_db_backup)" in enhanced_res.executed_steps
    assert enhanced_res.preview_plan["preflight_results"]["preflight_check_1"]["wallet_balance"] == 1500
    assert "Self-Healed Action Proposal" in enhanced_res.format_markdown()
    assert "Audit Signature" in enhanced_res.format_markdown()


def test_patch_all_activation():
    patch_all()  # Verifies patch_all activates cleanly without errors
