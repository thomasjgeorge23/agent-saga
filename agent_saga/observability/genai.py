"""OpenTelemetry GenAI semantic conventions, so existing dashboards just work.

agent-saga has always emitted good spans -- under `saga.*` attribute names it
invented. That means every observability tool in the world receives them and
understands none of them: Langfuse, Arize Phoenix, Datadog LLM Observability,
Grafana and Honeycomb all key their LLM views off OpenTelemetry's `gen_ai.*`
conventions, and a span that does not speak them shows up as an anonymous
rectangle.

This module speaks them. Install agent-saga behind an existing OTel pipeline and
token counts, model names, tool calls and finish reasons land in the panels that
are already built for them, with no configuration. That is worth more than any
feature, because it is the difference between "another library to instrument"
and "it already works".

**Additive, never a rename.** The `saga.*` attributes stay exactly as they are.
They carry what the conventions have no word for -- compensation semantics,
whether a span is an undo, the saga id that ties a rollback to its forward path
-- and something is already reading them. New vocabulary is added alongside; no
existing dashboard breaks.

**Message content is off by default, and that is a privacy decision.** The
conventions allow capturing prompts and completions on the span. Doing that
ships customer data to whoever runs your tracing backend, which is usually a
third party, forever, in a system nobody classified as a data store. OTel gates
it behind an opt-in and so does this: set
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` deliberately, or pass
`capture_content=True`, and nothing before that moment records a prompt.

**On stability:** the GenAI conventions are marked experimental upstream and
still moving. The subset used here is the stable core -- system, operation,
model, usage, tool identity, error type -- chosen because it is what the
dashboards actually key on and least likely to be renamed. `SEMCONV_VERSION`
records what this targets, so a future drift is a diff rather than a mystery.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Mapping, Optional, Sequence

__all__ = [
    "SEMCONV_VERSION",
    "capture_content_enabled",
    "error_attributes",
    "llm_span_attributes",
    "llm_span_name",
    "tool_span_attributes",
    "tool_span_name",
]

SEMCONV_VERSION = "1.27.0"
"""The OpenTelemetry semantic-convention release this targets. Recorded so a
later divergence is visible instead of silent."""

# -- attribute names (OTel GenAI semantic conventions) ---------------------------
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
GEN_AI_REQUEST_TEMPERATURE = "gen_ai.request.temperature"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"
GEN_AI_TOOL_DESCRIPTION = "gen_ai.tool.description"
GEN_AI_INPUT_MESSAGES = "gen_ai.input.messages"
GEN_AI_OUTPUT_MESSAGES = "gen_ai.output.messages"
ERROR_TYPE = "error.type"

#: `gen_ai.operation.name` values used here.
OP_CHAT = "chat"
OP_EXECUTE_TOOL = "execute_tool"

_CONTENT_ENV = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"

#: Provider names the conventions define. Anything else passes through as-is
#: rather than being forced into a bucket it does not belong in.
_KNOWN_SYSTEMS = {
    "openai": "openai", "azure": "az.ai.openai", "anthropic": "anthropic",
    "claude": "anthropic", "bedrock": "aws.bedrock", "vertex": "gcp.vertex_ai",
    "gemini": "gcp.gemini", "cohere": "cohere", "mistral": "mistral_ai",
    "ollama": "ollama", "groq": "groq", "deepseek": "deepseek",
}


def capture_content_enabled(explicit: Optional[bool] = None) -> bool:
    """Whether prompts and completions may be recorded on spans.

    False unless somebody said otherwise. An `explicit` argument wins over the
    environment so a caller can force it off for one sensitive call even where
    the deployment has it on -- the safe direction should always be reachable
    locally.
    """
    if explicit is not None:
        return explicit
    return os.getenv(_CONTENT_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _system(provider: Optional[str]) -> str:
    """Map a provider label onto a convention value, or keep it verbatim.

    Guessing wrong is worse than passing through: a dashboard filtering on
    `gen_ai.system = "anthropic"` should not silently include a local model
    because its name happened to contain a substring.
    """
    if not provider:
        return "unknown"
    lowered = provider.strip().lower()
    if lowered in _KNOWN_SYSTEMS:
        return _KNOWN_SYSTEMS[lowered]
    for key, value in _KNOWN_SYSTEMS.items():
        if lowered.startswith(key):
            return value
    return provider


def llm_span_name(model: Optional[str], operation: str = OP_CHAT) -> str:
    """`{operation} {model}` -- the convention's span name, which is what group
    -by-operation views in every backend are built around."""
    return f"{operation} {model}" if model else operation


def tool_span_name(tool: str) -> str:
    return f"{OP_EXECUTE_TOOL} {tool}"


def llm_span_attributes(
    *,
    provider: Optional[str] = None,
    request_model: Optional[str] = None,
    response_model: Optional[str] = None,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    finish_reason: Optional[str] = None,
    operation: str = OP_CHAT,
    input_messages: Optional[Sequence[Mapping[str, Any]]] = None,
    output_text: Optional[str] = None,
    capture_content: Optional[bool] = None,
) -> Dict[str, Any]:
    """Attributes for one model call.

    Only values that are actually known are emitted. A zero token count reported
    by a provider that did not report usage is not zero usage, and writing it as
    zero would put a wrong number into someone's cost dashboard -- so absent
    stays absent.
    """
    attributes: Dict[str, Any] = {
        GEN_AI_SYSTEM: _system(provider),
        GEN_AI_OPERATION_NAME: operation,
    }
    if request_model:
        attributes[GEN_AI_REQUEST_MODEL] = request_model
    if response_model:
        attributes[GEN_AI_RESPONSE_MODEL] = response_model
    if max_tokens is not None:
        attributes[GEN_AI_REQUEST_MAX_TOKENS] = max_tokens
    if temperature is not None:
        attributes[GEN_AI_REQUEST_TEMPERATURE] = temperature
    if input_tokens:
        attributes[GEN_AI_USAGE_INPUT_TOKENS] = input_tokens
    if output_tokens:
        attributes[GEN_AI_USAGE_OUTPUT_TOKENS] = output_tokens
    if finish_reason:
        # The convention models this as a list: a response can stop for more
        # than one reason across choices.
        attributes[GEN_AI_RESPONSE_FINISH_REASONS] = [finish_reason]

    if capture_content_enabled(capture_content):
        if input_messages is not None:
            attributes[GEN_AI_INPUT_MESSAGES] = _render_messages(input_messages)
        if output_text is not None:
            attributes[GEN_AI_OUTPUT_MESSAGES] = output_text
    return attributes


def tool_span_attributes(
    *,
    tool: str,
    call_id: Optional[str] = None,
    description: Optional[str] = None,
    arguments: Optional[Mapping[str, Any]] = None,
    capture_content: Optional[bool] = None,
) -> Dict[str, Any]:
    """Attributes for one tool execution.

    Arguments count as message content: a tool call carries the customer's
    address as readily as a prompt does, so they follow the same opt-in.
    """
    attributes: Dict[str, Any] = {
        GEN_AI_OPERATION_NAME: OP_EXECUTE_TOOL,
        GEN_AI_TOOL_NAME: tool,
    }
    if call_id:
        attributes[GEN_AI_TOOL_CALL_ID] = call_id
    if description:
        attributes[GEN_AI_TOOL_DESCRIPTION] = description
    if arguments is not None and capture_content_enabled(capture_content):
        attributes[GEN_AI_INPUT_MESSAGES] = _render(arguments)
    return attributes


def error_attributes(exc: BaseException) -> Dict[str, Any]:
    """`error.type` is what every backend's error-rate panel counts. The type
    name, not the message: a message carrying an account number would land in
    the tracing backend by the back door."""
    return {ERROR_TYPE: type(exc).__name__}


def _render_messages(messages: Sequence[Mapping[str, Any]]) -> str:
    import json

    try:
        return json.dumps(
            [{"role": str(m.get("role", "")), "content": str(m.get("content", ""))}
             for m in messages], ensure_ascii=False)[:8192]
    except Exception:
        return "<unrenderable>"


def _render(value: Any) -> str:
    import json

    try:
        return json.dumps(value, default=str, ensure_ascii=False)[:8192]
    except Exception:
        return "<unrenderable>"
