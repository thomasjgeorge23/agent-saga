"""OTel GenAI semantic conventions: the spans other people's tools understand.

agent-saga emitted good spans under attribute names it invented, which meant
every observability backend received them and understood none of them. These
tests pin the two properties that make the fix worth having:

1. **Additive, never a rename.** The `saga.*` attributes still carry what the
   conventions have no word for -- compensation semantics, whether a span is an
   undo -- and something is already reading them.
2. **Message content is off by default.** Prompts and tool arguments carry
   customer data, and a tracing backend is usually a third party that nobody
   classified as a data store.
"""

import os

import pytest
from conftest import aio

from agent_saga import ActionSemantics, AsyncWAL, Compensation, saga_scope
from agent_saga.observability.genai import (
    ERROR_TYPE,
    GEN_AI_INPUT_MESSAGES,
    GEN_AI_OPERATION_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_SYSTEM,
    GEN_AI_TOOL_CALL_ID,
    GEN_AI_TOOL_NAME,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    SEMCONV_VERSION,
    capture_content_enabled,
    error_attributes,
    llm_span_attributes,
    llm_span_name,
    tool_span_attributes,
    tool_span_name,
)

C = ActionSemantics.COMPENSABLE


@pytest.fixture(autouse=True)
def _content_off(monkeypatch):
    """Every test starts from the safe default, whatever the machine has set."""
    monkeypatch.delenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
                       raising=False)


# -- the vocabulary backends actually key on ----------------------------------------

def test_llm_attributes_use_the_convention_names():
    attributes = llm_span_attributes(
        provider="anthropic", request_model="claude-x", response_model="claude-x",
        input_tokens=120, output_tokens=30, finish_reason="stop",
        max_tokens=1000, temperature=0.2)

    assert attributes[GEN_AI_SYSTEM] == "anthropic"
    assert attributes[GEN_AI_OPERATION_NAME] == "chat"
    assert attributes[GEN_AI_REQUEST_MODEL] == "claude-x"
    assert attributes[GEN_AI_USAGE_INPUT_TOKENS] == 120
    assert attributes[GEN_AI_USAGE_OUTPUT_TOKENS] == 30
    assert attributes["gen_ai.response.finish_reasons"] == ["stop"]


def test_unknown_usage_is_absent_rather_than_zero():
    """A provider that reported nothing did not report zero. Writing zero puts a
    wrong number in somebody's cost dashboard."""
    attributes = llm_span_attributes(provider="ollama", request_model="llama",
                                     input_tokens=0, output_tokens=0)
    assert GEN_AI_USAGE_INPUT_TOKENS not in attributes
    assert GEN_AI_USAGE_OUTPUT_TOKENS not in attributes


def test_provider_names_map_to_convention_values_without_guessing():
    assert llm_span_attributes(provider="claude")[GEN_AI_SYSTEM] == "anthropic"
    assert llm_span_attributes(provider="bedrock")[GEN_AI_SYSTEM] == "aws.bedrock"
    # something unrecognised passes through verbatim rather than being forced
    # into a bucket a dashboard filter would then wrongly include
    assert llm_span_attributes(provider="acme-internal")[GEN_AI_SYSTEM] == "acme-internal"
    assert llm_span_attributes(provider=None)[GEN_AI_SYSTEM] == "unknown"


def test_span_names_follow_the_convention():
    assert llm_span_name("gpt-4o") == "chat gpt-4o"
    assert llm_span_name(None) == "chat"
    assert tool_span_name("stripe.charge") == "execute_tool stripe.charge"


def test_tool_attributes_carry_identity():
    attributes = tool_span_attributes(tool="stripe.charge", call_id="step-1",
                                      description="Charge a card.")
    assert attributes[GEN_AI_OPERATION_NAME] == "execute_tool"
    assert attributes[GEN_AI_TOOL_NAME] == "stripe.charge"
    assert attributes[GEN_AI_TOOL_CALL_ID] == "step-1"


def test_error_type_is_the_class_not_the_message():
    """An exception message can carry an account number; the type name cannot.
    Error-rate panels count the type anyway."""
    attributes = error_attributes(ValueError("card 4111-1111-1111-1111 declined"))
    assert attributes[ERROR_TYPE] == "ValueError"
    assert "4111" not in str(attributes)


def test_the_targeted_semconv_version_is_recorded():
    """So a later upstream rename is a diff rather than a mystery."""
    assert SEMCONV_VERSION


# -- content capture is opt-in ---------------------------------------------------------

def test_prompts_are_not_captured_by_default():
    attributes = llm_span_attributes(
        provider="openai", request_model="gpt-4o",
        input_messages=[{"role": "user", "content": "my card is 4111111111111111"}])
    assert GEN_AI_INPUT_MESSAGES not in attributes
    assert "4111" not in str(attributes)


def test_tool_arguments_are_not_captured_by_default():
    """A tool call carries a customer's address as readily as a prompt does."""
    attributes = tool_span_attributes(tool="ship.order",
                                      arguments={"address": "10 Downing Street"})
    assert GEN_AI_INPUT_MESSAGES not in attributes
    assert "Downing" not in str(attributes)


def test_content_capture_can_be_enabled_explicitly():
    attributes = llm_span_attributes(
        provider="openai", request_model="gpt-4o", capture_content=True,
        input_messages=[{"role": "user", "content": "hello"}])
    assert "hello" in attributes[GEN_AI_INPUT_MESSAGES]


def test_the_environment_variable_enables_it(monkeypatch):
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    assert capture_content_enabled() is True
    attributes = tool_span_attributes(tool="t", arguments={"k": "v"})
    assert GEN_AI_INPUT_MESSAGES in attributes


def test_an_explicit_false_beats_the_environment(monkeypatch):
    """The safe direction must always be reachable locally, even where the
    deployment has capture switched on."""
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    assert capture_content_enabled(False) is False
    attributes = tool_span_attributes(tool="t", arguments={"secret": "s"},
                                      capture_content=False)
    assert GEN_AI_INPUT_MESSAGES not in attributes


# -- additive: the saga vocabulary survives -----------------------------------------------

@aio
async def test_a_tool_span_carries_both_vocabularies(tmp_path):
    """The conventions have no word for compensation semantics, and something
    is already reading `saga.*`. Both are emitted."""
    seen = {}

    class Recorder:
        def span(self, name, attributes=None):
            seen["name"] = name
            seen["attributes"] = dict(attributes or {})
            import contextlib
            return contextlib.nullcontext()

        def correlation(self):
            return None, None

    import agent_saga.observability.otel as otel_module

    original = otel_module.get_tracer
    otel_module.get_tracer = lambda: Recorder()
    try:
        wal = AsyncWAL(tmp_path / "w.wal")
        await wal.start()
        try:
            async with saga_scope(wal=wal, name="t") as saga:
                await saga.execute(
                    tool="stripe.charge", semantics=C,
                    forward=lambda amount: {"id": "ch_1"},
                    forward_kwargs={"amount": 4200},
                    compensate=lambda r: Compensation(fn=lambda: None,
                                                      description="refund"))
        finally:
            await wal.close()
    finally:
        otel_module.get_tracer = original

    attributes = seen["attributes"]
    # the conventions
    assert attributes[GEN_AI_TOOL_NAME] == "stripe.charge"
    assert attributes[GEN_AI_OPERATION_NAME] == "execute_tool"
    # and the saga-native attributes, unchanged
    assert attributes["saga.tool"] == "stripe.charge"
    assert attributes["saga.semantics"] == "COMPENSABLE"
    assert attributes["saga.is_compensation"] is False
    # arguments still withheld by default
    assert "4200" not in str(attributes.get(GEN_AI_INPUT_MESSAGES, ""))
