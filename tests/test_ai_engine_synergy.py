import pytest

from agent_saga import (
    ContextSanitizer,
    LoopEntropyDetector,
    SemanticOutputVerifier,
    UniversalToolAdapter,
)


def test_semantic_output_verifier():
    verifier = SemanticOutputVerifier(
        validators=[lambda data: (data.get("status") == "success", "status must be success")]
    )

    # Valid output
    v1 = verifier.verify({"status": "success", "result": 42})
    assert v1.is_valid

    # Soft error in dictionary
    v2 = verifier.verify({"error": "Rate limit exceeded"})
    assert not v2.is_valid
    assert "Soft error" in v2.reason

    # Failed status validator
    v3 = verifier.verify({"status": "failed", "message": "DB error"})
    assert not v3.is_valid


def test_context_sanitizer():
    history = [
        {"type": "STEP_INTENT", "tool": "payment"},
        {"type": "STEP_COMMITTED", "tool": "payment"},
        {"type": "STEP_UNKNOWN", "tool": "notification"},  # Failed trial
        {"type": "COMPLETED_VIA_FALLBACK", "tool": "notification_fallback"},
    ]

    pruned = ContextSanitizer.prune_failed_trials(history)
    assert len(pruned) == 2
    types = [e["type"] for e in pruned]
    assert types == ["STEP_COMMITTED", "COMPLETED_VIA_FALLBACK"]


def test_loop_entropy_detector():
    detector = LoopEntropyDetector(max_repetition=3)

    ok1, _ = detector.check_call("search", '{"query": "python"}')
    assert not ok1

    ok2, _ = detector.check_call("search", '{"query": "python"}')
    assert not ok2

    # 3rd identical call triggers loop detection
    is_loop, reason = detector.check_call("search", '{"query": "python"}')
    assert is_loop
    assert "repeated 3 times" in reason


def test_universal_tool_adapter():
    raw_args = {
        "user_name": "Alice",
        "metadata": '{"role": "admin", "permissions": ["read", "write"]}',
    }
    normalized = UniversalToolAdapter.normalize_args(raw_args)
    assert normalized["user_name"] == "Alice"
    assert isinstance(normalized["metadata"], dict)
    assert normalized["metadata"]["role"] == "admin"
