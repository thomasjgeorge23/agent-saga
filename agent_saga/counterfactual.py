"""Counterfactual replay: would the cheaper model have got it right?

Every team that wants to swap a frontier model for a 7B faces the same wall.
You cannot test it on production traffic, because testing means *doing* the
side effects, twice, for real. So the decision gets made on a benchmark that
resembles nothing, or on vibes.

The WAL makes a third option possible, and as far as I can tell nobody has
built it for agents: a log of every tool call, its arguments, and the result it
returned **is a simulator of that afternoon's world**. Point a new candidate at
it and serve the recorded results instead of calling anything. You get to ask
"would this model have made the same calls?" against real history, at zero risk,
because nothing executes.

    verdict = await counterfactual_replay(records, saga_id, candidate=cheap_agent)
    print(verdict.format_text())

**No side effect can occur during a replay.** The replay context never invokes
a `forward` callable at all -- it matches the call against the recording and
returns what really happened. A test asserts this by handing it a forward that
raises if touched.

**The honest limit, stated rather than papered over.** The moment a candidate
calls a different tool, or the same tool with different arguments, there is no
recorded result for it and the recording cannot say what would have happened.
That is the fundamental boundary of off-policy evaluation, and pretending
otherwise is how offline evaluation gets a bad name. So a divergence is
reported as **UNKNOWABLE** and the replay stops there. "Matched on 7 of 9 steps,
then left the recording" is a true and useful sentence. "The 7B would have
succeeded" would not be.

What the verdict is good for: measuring how often a candidate stays on the
recorded path across many historical sagas. A model that reproduces the
frontier model's calls on 95% of last month's traffic is a defensible switch. A
model that diverges on the third step of everything is not, and you learned it
without charging anyone twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import (Any, Awaitable, Callable, Dict, List, Mapping, Optional,
                    Sequence, Tuple)

logger = logging.getLogger("agent_saga.counterfactual")

__all__ = [
    "Divergence",
    "RecordedStep",
    "ReplayVerdict",
    "SideEffectAttempted",
    "counterfactual_replay",
]

MATCHED = "MATCHED"
DIVERGED_ARGS = "DIVERGED_ARGS"
DIVERGED_TOOL = "DIVERGED_TOOL"
EXTRA_CALL = "EXTRA_CALL"
MISSING_CALLS = "MISSING_CALLS"


class SideEffectAttempted(RuntimeError):
    """A replay tried to actually execute something. Should be unreachable --
    it exists so that if the guard is ever broken, the replay fails loudly
    instead of quietly charging a real card."""


class _Stop(Exception):
    """Internal: unwinds the candidate once it has left the recording."""


@dataclass(frozen=True)
class RecordedStep:
    index: int
    tool: str
    kwargs: Mapping[str, Any]
    result: Any
    outcome: str                   # COMMITTED | UNKNOWN

    def describe(self) -> dict:
        return {"index": self.index, "tool": self.tool,
                "kwargs": dict(self.kwargs), "outcome": self.outcome}


@dataclass(frozen=True)
class Divergence:
    step: int
    kind: str
    recorded_tool: Optional[str] = None
    candidate_tool: Optional[str] = None
    detail: str = ""

    @property
    def knowable(self) -> bool:
        """Whether the recording can say what would have happened next.

        Only a match is knowable. Everything else is the boundary of what an
        offline recording can answer, and calling it anything else would be a
        guess dressed as a measurement.
        """
        return self.kind == MATCHED

    def describe(self) -> dict:
        return {"step": self.step, "kind": self.kind, "knowable": self.knowable,
                "recorded_tool": self.recorded_tool,
                "candidate_tool": self.candidate_tool, "detail": self.detail}


@dataclass(frozen=True)
class ReplayVerdict:
    saga_id: str
    recorded_steps: Tuple[RecordedStep, ...]
    matched: int
    divergences: Tuple[Divergence, ...]
    candidate_error: Optional[str] = None

    @property
    def faithful(self) -> bool:
        """The candidate reproduced the whole recorded path.

        Requires at least one recorded step: an empty recording proves nothing,
        and reporting `True` for it would be vacuous success.
        """
        return (bool(self.recorded_steps)
                and self.matched == len(self.recorded_steps)
                and not [d for d in self.divergences if not d.knowable])

    @property
    def match_rate(self) -> float:
        if not self.recorded_steps:
            return 0.0
        return self.matched / len(self.recorded_steps)

    @property
    def left_the_recording_at(self) -> Optional[int]:
        for divergence in self.divergences:
            if not divergence.knowable:
                return divergence.step
        return None

    def describe(self) -> dict:
        return {"saga_id": self.saga_id,
                "recorded_steps": len(self.recorded_steps),
                "matched": self.matched, "faithful": self.faithful,
                "match_rate": round(self.match_rate, 4),
                "left_the_recording_at": self.left_the_recording_at,
                "candidate_error": self.candidate_error,
                "divergences": [d.describe() for d in self.divergences]}

    def format_text(self) -> str:
        total = len(self.recorded_steps)
        lines = [f"counterfactual replay of saga {self.saga_id}",
                 f"  recorded steps : {total}",
                 f"  matched        : {self.matched}"
                 + (f" ({self.match_rate:.0%})" if total else "")]
        if not total:
            lines.append("")
            lines.append("  The recording has no committed steps, so nothing was")
            lines.append("  compared and nothing was proven.")
            return "\n".join(lines)

        for divergence in self.divergences:
            mark = "  ok  " if divergence.knowable else "  ->  "
            lines.append(f"{mark}step {divergence.step}: {divergence.kind}"
                         + (f"  {divergence.detail}" if divergence.detail else ""))

        boundary = self.left_the_recording_at
        lines.append("")
        if self.faithful:
            lines.append("  The candidate reproduced the recorded path exactly.")
        elif boundary is not None:
            lines.append(f"  The candidate left the recording at step {boundary}.")
            lines.append(f"  What would have happened after that is UNKNOWABLE from")
            lines.append(f"  this log -- no result was ever recorded for the call it")
            lines.append(f"  made. This is the boundary of offline evaluation, not a")
            lines.append(f"  verdict on the candidate.")
        if self.candidate_error:
            lines.append(f"  candidate raised: {self.candidate_error}")
        return "\n".join(lines)


class _ReplayContext:
    """A SagaContext-shaped stand-in that serves recordings and executes nothing."""

    def __init__(self, steps: Sequence[RecordedStep], saga_id: str):
        self._steps = list(steps)
        self.saga_id = f"replay:{saga_id}"
        self.position = 0
        self.divergences: List[Divergence] = []
        self.matched = 0

    async def execute(self, *, tool: str, semantics: Any = None,
                      forward: Any = None, forward_kwargs: Any = None,
                      compensate: Any = None, policy_args: Any = None,
                      timeout: Any = None, **_ignored: Any) -> Any:
        """Match the call against the recording and return what really
        happened. `forward` is never called -- that is the whole safety
        property, so it is not merely unused, it is deliberately unreachable."""
        kwargs = dict(forward_kwargs or {})
        index = self.position + 1

        if self.position >= len(self._steps):
            self.divergences.append(Divergence(
                step=index, kind=EXTRA_CALL, candidate_tool=tool,
                detail=f"called {tool!r} after the recording ended"))
            raise _Stop()

        recorded = self._steps[self.position]
        if recorded.tool != tool:
            self.divergences.append(Divergence(
                step=index, kind=DIVERGED_TOOL, recorded_tool=recorded.tool,
                candidate_tool=tool,
                detail=f"recording has {recorded.tool!r}, candidate called {tool!r}"))
            raise _Stop()

        if _normalise(kwargs) != _normalise(recorded.kwargs):
            self.divergences.append(Divergence(
                step=index, kind=DIVERGED_ARGS, recorded_tool=recorded.tool,
                candidate_tool=tool,
                detail=(f"same tool, different arguments: recorded "
                        f"{_normalise(recorded.kwargs)}, candidate "
                        f"{_normalise(kwargs)}")))
            raise _Stop()

        self.divergences.append(Divergence(
            step=index, kind=MATCHED, recorded_tool=tool, candidate_tool=tool))
        self.matched += 1
        self.position += 1
        return recorded.result


async def counterfactual_replay(
    records: Sequence[Mapping[str, Any]],
    saga_id: str,
    candidate: Callable[[Any], Awaitable[Any]],
) -> ReplayVerdict:
    """Run `candidate` against a recorded saga without executing anything.

    `candidate(ctx)` should perform its work through `ctx.execute(tool=...,
    forward=..., forward_kwargs=...)`, exactly as it would in a real saga. The
    context serves recorded results and refuses to invoke any forward callable.
    """
    steps = _recorded_steps(records, saga_id)
    ctx = _ReplayContext(steps, saga_id)
    error: Optional[str] = None

    try:
        await candidate(ctx)
    except _Stop:
        pass                            # the candidate left the recording
    except SideEffectAttempted:
        raise
    except Exception as exc:            # the candidate's own failure is data
        error = repr(exc)

    divergences = list(ctx.divergences)
    if (ctx.position < len(steps) and not [d for d in divergences if not d.knowable]
            and error is None):
        divergences.append(Divergence(
            step=ctx.position + 1, kind=MISSING_CALLS,
            recorded_tool=steps[ctx.position].tool,
            detail=(f"the candidate stopped after {ctx.position} step(s); the "
                    f"recording has {len(steps)}")))

    return ReplayVerdict(saga_id=saga_id, recorded_steps=tuple(steps),
                         matched=ctx.matched, divergences=tuple(divergences),
                         candidate_error=error)


# -- reading the recording ------------------------------------------------------------

def _recorded_steps(records: Sequence[Mapping[str, Any]],
                    saga_id: str) -> List[RecordedStep]:
    """Committed and UNKNOWN steps in order, with the arguments the WAL kept.

    Arguments come from `STEP_INTENT`, which stores a JSON-safe projection --
    non-serialisable values were repr'd on the way in. Comparison therefore
    happens on that projection, which is what the log can actually attest to.
    """
    intents: Dict[str, Mapping[str, Any]] = {}
    steps: List[RecordedStep] = []

    for record in records:
        if not isinstance(record, Mapping) or record.get("saga_id") != saga_id:
            continue
        event = record.get("event")
        step_id = record.get("step_id")
        if event == "STEP_INTENT" and step_id:
            intents[str(step_id)] = dict(record.get("kwargs") or {})
        elif event in ("STEP_COMMITTED", "STEP_UNKNOWN") and step_id:
            steps.append(RecordedStep(
                index=len(steps) + 1,
                tool=str(record.get("tool", "unknown")),
                kwargs=intents.get(str(step_id), {}),
                # The WAL records the compensation descriptor, not the forward
                # result, so a replay can attest to the CALL but not to the
                # returned payload. Callers comparing results should record them
                # deliberately; this returns None rather than inventing one.
                result=record.get("result"),
                outcome="COMMITTED" if event == "STEP_COMMITTED" else "UNKNOWN"))
    return steps


def _normalise(value: Any) -> Any:
    """Compare arguments the way the log stored them: JSON-ish, sorted, with
    everything else reduced to a bounded repr."""
    if isinstance(value, Mapping):
        return {str(k): _normalise(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalise(v) for v in value]
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return repr(value)[:256]
