"""The agent loop: a frontier-grade harness for whatever model you have.

What makes a frontier assistant feel capable is only partly the model. The
rest is harness: context discipline, tool orchestration, bounded repair,
honest stopping, and recovery when something breaks mid-task. The model's
intelligence is not transferable; the harness is. This module is that
harness, built from the pieces this package already proves:

    ContextBroker   what the model sees is budgeted, receipted, WAL-recorded
    Router          which host serves is deterministic and truthfully reported
    SagaContext     what the model does is gated, logged, and compensable
    LoopEntropy     when the model thrashes, the loop says so and stops

The one doctrinal decision, stated where it can be argued with:

    **The goal is the transaction.** `run()` returns only on completion. A
    stall, an exhausted step budget, or a failing tool raises -- and because
    the loop runs inside your saga scope, that raise unwinds every side
    effect the attempt already caused. An agent that gave up halfway does not
    leave three of five steps loose in the world; it leaves a compensated
    transaction and a WAL that shows exactly what happened. Partial progress
    you want to keep is a policy for a future revision to make explicit --
    today's honest default is all-or-nothing.

What this module does NOT claim: it does not make the host model smarter.
A 7B model in this loop is still a 7B model -- it is just budgeted, audited,
repaired once when it emits malformed actions, stopped when it thrashes, and
rolled back when it fails. That is the entire promise, and it is kept.

Tool-call protocol: plain JSON over text, one action per turn --
``{"action": "tool", "tool": <name>, "args": {...}}`` or
``{"action": "final", "answer": "..."}``. Text, not native tool-calling,
because the loop must work on hosts that never heard of tool schemas; hosts
with native support lose nothing. Malformed output gets exactly one repair
round (the router's machinery, carrying the parse error back to the model),
then fails loudly.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from .ai_engine import LoopEntropyDetector, UniversalToolAdapter
from .context_broker import ContextBroker
from .ir import ChatRequest, ChatResponse, system, user
from .observability.otel import get_tracer
from .router import Router
from .semantics import ActionSemantics, CompensationFactory

logger = logging.getLogger("agent_saga.agent_loop")

_NUDGE = "Respond with exactly one JSON action object and nothing else."

_PROTOCOL = """\
You operate tools by replying with EXACTLY ONE JSON object per turn:
  {{"action": "tool", "tool": "<name>", "args": {{...}}}}   to use a tool
  {{"action": "final", "answer": "<your answer>"}}          when done
Available tools:
{catalog}"""


class AgentLoopError(RuntimeError):
    """Base for every way the loop can end other than completion. Raised INSIDE
    the saga scope on purpose: unwinding the attempt's side effects is the
    default consequence of not finishing."""


class LoopStalled(AgentLoopError):
    def __init__(self, reason: str, steps: Sequence["StepRecord"]):
        self.reason, self.steps = reason, tuple(steps)
        super().__init__(f"agent loop stalled after {len(self.steps)} step(s): {reason}")


class LoopExhausted(AgentLoopError):
    def __init__(self, max_steps: int, steps: Sequence["StepRecord"]):
        self.steps = tuple(steps)
        super().__init__(
            f"agent loop spent its budget of {max_steps} steps without reaching "
            f"a final answer; refusing to keep spending, refusing to fake one")


class LoopDeadlineExceeded(AgentLoopError):
    def __init__(self, deadline_s: float, elapsed: float, steps: Sequence["StepRecord"]):
        self.steps = tuple(steps)
        super().__init__(
            f"agent loop exceeded its wall-clock deadline of {deadline_s}s "
            f"(elapsed {elapsed:.1f}s) after {len(self.steps)} step(s)")


class LoopBudgetExceeded(AgentLoopError):
    def __init__(self, max_tokens: int, used: int, steps: Sequence["StepRecord"]):
        self.steps = tuple(steps)
        super().__init__(
            f"agent loop spent ~{used} tokens against a budget of {max_tokens} "
            f"after {len(self.steps)} step(s); refusing to keep spending")


@dataclass(frozen=True)
class LoopTool:
    """A tool the model may invoke. Semantics are REQUIRED -- the whole library
    exists because 'what kind of side effect is this?' must be answered by the
    author, not guessed by the engine."""

    name: str
    description: str
    fn: Callable[..., Any]
    semantics: ActionSemantics
    parameters: Mapping[str, Any] = field(default_factory=dict)   # JSON Schema
    compensate: Optional[CompensationFactory] = None
    timeout: Optional[float] = None
    """Per-call deadline, passed through to `ctx.execute`. A tool with no
    deadline inherits the saga's default; a tool that can hang needs its own."""


@dataclass(frozen=True)
class StepRecord:
    step: int
    tool: str
    args: Mapping[str, Any]


@dataclass(frozen=True)
class LoopResult:
    """Returned ONLY for a completed goal. Every other ending raises."""

    status: str                    # always "COMPLETED"
    answer: str
    steps: Tuple[StepRecord, ...]
    tokens_used: int = 0           # provider-reported where available, else estimated
    elapsed_s: float = 0.0


@dataclass(frozen=True)
class _Action:
    kind: str                      # "tool" | "final"
    tool: str = ""
    args: Mapping[str, Any] = field(default_factory=dict)
    answer: str = ""


class AgentLoop:
    """Plan/act/observe against any Router host, inside a saga.

    Dependency-injected end to end: the router decides who serves, the broker
    decides what is seen, the caller's SagaContext decides what execution
    means. The loop owns only the discipline between them.
    """

    def __init__(
        self,
        router: Router,
        broker: ContextBroker,
        tools: Sequence[LoopTool],
        *,
        max_steps: int = 8,
        budget_tokens: int = 2000,
        max_repeated_calls: int = 3,
        deadline_s: Optional[float] = None,
        max_total_tokens: Optional[int] = None,
    ):
        names = [t.name for t in tools]
        if len(set(names)) != len(names):
            raise ValueError(f"tool names must be unique, got {names}")
        # Constructor-time validation, because a loop misconfigured into
        # never-stopping or never-starting must fail at the desk, not at 3am.
        if max_steps < 1:
            raise ValueError(f"max_steps must be >= 1, got {max_steps}")
        if budget_tokens < 1:
            raise ValueError(f"budget_tokens must be >= 1, got {budget_tokens}")
        if max_repeated_calls < 1:
            raise ValueError(f"max_repeated_calls must be >= 1, got {max_repeated_calls}")
        if deadline_s is not None and deadline_s < 0:
            raise ValueError(f"deadline_s must be >= 0, got {deadline_s}")
        if max_total_tokens is not None and max_total_tokens < 1:
            raise ValueError(f"max_total_tokens must be >= 1, got {max_total_tokens}")
        self.router = router
        self.broker = broker
        self.tools: Dict[str, LoopTool] = {t.name: t for t in tools}
        self.max_steps = max_steps
        self.budget_tokens = budget_tokens
        self.max_repeated_calls = max_repeated_calls
        self.deadline_s = deadline_s
        """Wall-clock ceiling for one run(). Checked before every model call
        and every tool call; exceeding it raises (and therefore unwinds)."""
        self.max_total_tokens = max_total_tokens
        """Cumulative token ceiling for one run(). Provider-reported usage
        where available; the chars/4 estimate where the provider reports
        nothing -- enforcement on an estimate beats no enforcement, and which
        one was used is visible per step in the AGENT_DECISION events."""

        # The catalog and protocol travel as pack(extra_prefix=...): byte-stable
        # on every step (prompt caches keep hitting, CONTEXT_PACKED hashes stay
        # comparable) WITHOUT mutating the caller's broker -- the loop borrows
        # the broker, it does not own it.
        catalog = "\n".join(
            f'  {t.name}: {t.description} args={json.dumps(dict(t.parameters), sort_keys=True)}'
            for t in sorted(self.tools.values(), key=lambda t: t.name))
        self._protocol_block = _PROTOCOL.format(catalog=catalog)

    # -- the loop ---------------------------------------------------------------

    async def run(self, goal: str, ctx: Any) -> LoopResult:
        started = time.monotonic()
        tokens_used = 0
        self.broker.push_hot(f"GOAL: {goal}")
        detector = LoopEntropyDetector(max_repetition=self.max_repeated_calls)
        steps: list[StepRecord] = []

        def check_deadline() -> None:
            # >= so deadline_s=0.0 means "no time at all" even on clocks with
            # coarse granularity (Windows monotonic pre-3.13 ticks at ~15ms).
            elapsed = time.monotonic() - started
            if self.deadline_s is not None and elapsed >= self.deadline_s:
                raise LoopDeadlineExceeded(self.deadline_s, elapsed, steps)

        def check_tokens() -> None:
            # Checked only where tokens are spent (before a model call). A
            # pre-tool token check would discard an already-decided action
            # while saving nothing -- tools cost no tokens.
            if self.max_total_tokens is not None and tokens_used >= self.max_total_tokens:
                raise LoopBudgetExceeded(self.max_total_tokens, tokens_used, steps)

        for step_no in range(1, self.max_steps + 1):
            check_deadline()
            check_tokens()
            # One span per step, parenting the router call and (via
            # context.execute) the tool span -- a trace of a run reads as
            # steps, each with its model call and its side effect inside.
            with get_tracer().span("agent.loop.step", {"agent.step": step_no}):
                packed = self.broker.pack(self.budget_tokens,
                                          extra_prefix=self._protocol_block)
                request = ChatRequest(messages=(system(packed.text), user(_NUDGE)))
                response, _decision = await self.router.complete(
                    request, ctx=ctx, validate=self._validate, max_repair_rounds=1)
                action = self._parse(response.text)

                reported = response.usage.input_tokens + response.usage.output_tokens
                tokens_used += reported if reported > 0 else (
                    request.approx_tokens() + _approx(response.text))

                # Tie the decision to the exact context that produced it. This is
                # the line that makes "why did the agent do that?" a lookup, not a
                # reconstruction: AGENT_DECISION.context_hash == CONTEXT_PACKED.
                ctx.wal.append("AGENT_DECISION", {
                    "saga_id": getattr(ctx, "saga_id", None),
                    "step": step_no,
                    "context_hash": packed.content_hash,
                    "host": response.provider,
                    "action": action.kind,
                    "tool": action.tool,
                    "args": _jsonable(action.args),
                    "elapsed_s": round(time.monotonic() - started, 3),
                    "tokens_used": tokens_used,
                    "tokens_reported_by_provider": reported > 0,
                })

                if action.kind == "final":
                    return LoopResult(status="COMPLETED", answer=action.answer,
                                      steps=tuple(steps), tokens_used=tokens_used,
                                      elapsed_s=round(time.monotonic() - started, 3))

                looping, why = detector.check_call(
                    action.tool, json.dumps(_jsonable(action.args), sort_keys=True))
                if looping:
                    raise LoopStalled(why, steps)

                check_deadline()
                tool = self.tools[action.tool]
                args = UniversalToolAdapter.normalize_args(dict(action.args))
                result = await ctx.execute(
                    tool=tool.name,
                    semantics=tool.semantics,
                    forward=tool.fn,
                    forward_kwargs=args,
                    compensate=tool.compensate,
                    timeout=tool.timeout,
                )
                steps.append(StepRecord(step=step_no, tool=tool.name, args=args))
                self.broker.push_hot(
                    f"TOOL {tool.name} -> {json.dumps(_jsonable(result), default=str)[:500]}")

        raise LoopExhausted(self.max_steps, steps)

    # -- protocol parsing ----------------------------------------------------------

    def _validate(self, response: ChatResponse) -> None:
        """Router-shaped validator: raises with a reason the model can act on.
        The router carries that reason back in its single bounded repair round."""
        self._parse(response.text)

    def _parse(self, text: str) -> _Action:
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else ""
            raw = raw.rsplit("```", 1)[0].strip()
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"reply must be one JSON action object, got unparseable text ({exc})")
        if not isinstance(doc, dict) or doc.get("action") not in ("tool", "final"):
            raise ValueError(
                'reply must be {"action": "tool", ...} or {"action": "final", ...}')
        if doc["action"] == "final":
            return _Action(kind="final", answer=str(doc.get("answer", "")))
        name = doc.get("tool")
        if name not in self.tools:
            raise ValueError(
                f"unknown tool {name!r}; the available tools are: "
                f"{sorted(self.tools)}")
        args = doc.get("args", {})
        if not isinstance(args, dict):
            raise ValueError('"args" must be a JSON object')
        return _Action(kind="tool", tool=name, args=args)


def _approx(text: str) -> int:
    """chars/4, the package's uniform admission-control estimate."""
    return max(1, len(text) // 4)


def _jsonable(value: Any) -> Any:
    """Best-effort JSON projection for WAL/HOT bookkeeping. Never raises on the
    logging path; opaque objects degrade to bounded reprs."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return repr(value)[:512]


__all__ = [
    "AgentLoop",
    "AgentLoopError",
    "LoopBudgetExceeded",
    "LoopDeadlineExceeded",
    "LoopExhausted",
    "LoopResult",
    "LoopStalled",
    "LoopTool",
    "StepRecord",
]
