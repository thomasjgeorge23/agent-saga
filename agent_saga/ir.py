"""Provider-neutral intermediate representation for LLM calls.

The saga core, gates, router, and ledger speak these types and only these
types; adapters translate them to and from each vendor SDK at the edge. That
is the entire trick -- and it is worth being precise about what it buys:

  * **Portability, not intelligence.** Normalizing a request so it can run on
    a local 7B model or a frontier API does not make either model smarter. It
    makes the *plumbing* -- routing, retry, breaker state, audit -- identical
    across hosts, so reliability work done once applies everywhere.
  * **Lossless edges.** Anything the IR does not model rides `provider_extra`
    untouched, both directions. An IR that silently drops vendor fields is a
    normalization layer lying about being complete.
  * **Declared, not measured.** `Capabilities` records what a provider *says*
    about a host (context length, tool support). The router treats these as
    hints and records every decision, so a wrong declaration is diagnosable
    from the log rather than silently misrouting forever.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

#: Documented role values. A plain str, not an enum: providers keep inventing
#: roles, and an IR that raises on an unknown role forces every new vendor
#: feature to wait for a core release. Unknown roles pass through.
ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"


@dataclass(frozen=True)
class Message:
    role: str
    content: str
    name: Optional[str] = None
    tool_call_id: Optional[str] = None   # links a ROLE_TOOL result to its call


@dataclass(frozen=True)
class ToolSpec:
    """A tool offered to the model. `parameters` is a JSON-Schema dict, the
    lingua franca every current provider accepts after adapter translation."""

    name: str
    description: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation the model asked for. Executing it is the caller's
    job -- through `AgentKit.safe_tool` / `ctx.execute`, where semantics and
    compensation live. The IR carries intent only."""

    id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Usage:
    """Token counts as reported by the provider; zeros mean 'not reported',
    never 'free'."""

    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class ChatRequest:
    messages: Tuple[Message, ...]
    tools: Tuple[ToolSpec, ...] = ()
    json_only: bool = False          # request strict-JSON output where supported
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    provider_extra: Mapping[str, Any] = field(default_factory=dict)

    def approx_tokens(self) -> int:
        """Crude size estimate (~4 chars/token) for admission control and
        context-fit routing. It is an ESTIMATE: good enough to keep a 300k-char
        payload away from an 8k-context host, useless for billing. Anything
        that needs a real count must use the provider's tokenizer."""
        chars = sum(len(m.content) for m in self.messages)
        chars += sum(len(t.name) + len(t.description) + len(str(t.parameters))
                     for t in self.tools)
        return math.ceil(chars / 4)


@dataclass(frozen=True)
class ChatResponse:
    text: str
    provider: str
    model: str
    tool_calls: Tuple[ToolCall, ...] = ()
    finish_reason: str = "stop"
    usage: Usage = field(default_factory=Usage)
    provider_extra: Mapping[str, Any] = field(default_factory=dict)


class CostClass(enum.Enum):
    """Coarse routing signal. Three buckets on purpose: a per-token price table
    goes stale the week it ships; an ordering of 'free-ish local', 'cheap
    hosted', 'premium hosted' does not."""

    LOCAL = 0
    ECONOMY = 1
    PREMIUM = 2


@dataclass(frozen=True)
class Capabilities:
    """Declared facts about one host. Declared -- the adapter author asserts
    them from provider documentation; nothing here is measured at runtime.
    The router records which declarations each decision relied on, so the day
    a declaration is wrong, the log shows exactly which routes it poisoned."""

    context_tokens: int
    supports_tools: bool = False
    supports_json_mode: bool = False
    supports_streaming: bool = False
    cost_class: CostClass = CostClass.ECONOMY

    def describe(self) -> dict:
        return {
            "context_tokens": self.context_tokens,
            "supports_tools": self.supports_tools,
            "supports_json_mode": self.supports_json_mode,
            "supports_streaming": self.supports_streaming,
            "cost_class": self.cost_class.name,
        }


@runtime_checkable
class HostAdapter(Protocol):
    """The contract a provider integration implements. One adapter per host
    (a model on a provider), registered with the router by unique name.

    `complete()` must translate IR -> native SDK -> IR without altering the
    model's output, and must let provider errors propagate as exceptions --
    the router owns fallback and breaker bookkeeping, and an adapter that
    swallows errors starves both.
    """

    @property
    def name(self) -> str: ...

    def capabilities(self) -> Capabilities: ...

    async def complete(self, request: ChatRequest) -> ChatResponse: ...


def user(content: str) -> Message:
    return Message(role=ROLE_USER, content=content)


def system(content: str) -> Message:
    return Message(role=ROLE_SYSTEM, content=content)


def assistant(content: str) -> Message:
    return Message(role=ROLE_ASSISTANT, content=content)


__all__ = [
    "ROLE_ASSISTANT",
    "ROLE_SYSTEM",
    "ROLE_TOOL",
    "ROLE_USER",
    "Capabilities",
    "ChatRequest",
    "ChatResponse",
    "CostClass",
    "HostAdapter",
    "Message",
    "ToolCall",
    "ToolSpec",
    "Usage",
    "assistant",
    "system",
    "user",
]
