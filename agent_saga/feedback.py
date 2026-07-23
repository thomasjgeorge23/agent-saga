"""Self-healing LLM prompt feedback loop.

Connects MissionCriticalGate and HallucinationDetector refusal events directly
to agent retry loops. Formats gate rejection verdicts into structured system prompt
instructions so the model automatically self-corrects on retry attempts.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .gate import Decision, Verdict

logger = logging.getLogger("agent_saga.feedback")


async def _acall(fn: Callable, *args) -> Any:
    """Call fn (sync or async) and return its result."""
    result = fn(*args)
    if inspect.isawaitable(result):
        return await result
    return result


@dataclass
class SelfHealingPromptFeedback:
    """Formats refusal and hallucination verdicts into actionable system prompt feedback."""

    tool: str
    reason: str
    hallucination_score: float = 0.0
    suggested_correction: str = ""

    def to_system_prompt_instruction(self) -> str:
        prompt = (
            f"[SYSTEM FEEDBACK - SAGA PROTECTION GATE REJECTION]\n"
            f"Your proposed action on tool {self.tool!r} was REJECTED by the transaction protection gate.\n"
            f"Reason: {self.reason}\n"
        )
        if self.hallucination_score > 0:
            prompt += f"Hallucination Risk Score: {self.hallucination_score:.2f}\n"
        if self.suggested_correction:
            prompt += f"Suggested Fix: {self.suggested_correction}\n"
        prompt += (
            "Please adjust your parameter arguments and reasoning to satisfy the safety constraint before calling this tool again."
        )
        return prompt

    @classmethod
    def from_decision(cls, tool: str, decision: Decision, hallucination_score: float = 0.0) -> Optional[SelfHealingPromptFeedback]:
        if decision.verdict == Verdict.ALLOW:
            return None
        return cls(
            tool=tool,
            reason=decision.reason,
            hallucination_score=hallucination_score,
            suggested_correction="Verify target account bounds and idempotency keys.",
        )


@dataclass
class HealingOutcome:
    """The result of a self-healing correction loop."""
    healed: bool
    attempts: int
    result: Any = None                 # the accepted proposal, when healed
    escalated: bool = False
    feedbacks: list = field(default_factory=list)
    final_context: str = ""
    final_reason: str = ""


class SelfHealingLoop:
    """Drive an agent through a bounded correction loop.

    On each attempt the agent proposes an action from the current context; a
    gate/hallucination check evaluates it. If the proposal is accepted the loop
    returns healed. If it is refused, the refusal is formatted into a
    :class:`SelfHealingPromptFeedback` instruction, *appended to the context*, and
    the agent is asked again -- with exponential backoff between attempts. After
    ``max_retries`` attempts without acceptance the loop escalates to a human
    (via ``on_escalate``) instead of looping forever.

        loop = SelfHealingLoop(max_retries=3)
        outcome = await loop.run(agent_fn, evaluate_fn, tool="stripe.charge")

    ``agent_fn(context) -> proposal`` and ``evaluate_fn(proposal) -> Decision``
    (or ``(Decision, hallucination_score)``) may be sync or async.
    """

    def __init__(self, *, max_retries: int = 3, base_delay: float = 0.5,
                 backoff_factor: float = 2.0, max_delay: float = 30.0,
                 hallucination_threshold: float = 0.7,
                 on_escalate: Optional[Callable[[HealingOutcome], Any]] = None,
                 sleep: Optional[Callable[[float], Awaitable[None]]] = None):
        if max_retries < 1:
            raise ValueError("max_retries must be >= 1")
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.backoff_factor = backoff_factor
        self.max_delay = max_delay
        self.hallucination_threshold = hallucination_threshold
        self.on_escalate = on_escalate
        self._sleep = sleep or asyncio.sleep

    async def run(self, agent_fn: Callable[[str], Any],
                  evaluate_fn: Callable[[Any], Any], *,
                  tool: str = "", context: str = "") -> HealingOutcome:
        feedbacks: list = []
        current = context
        attempts = 0

        for attempt in range(1, self.max_retries + 1):
            attempts = attempt
            proposal = await _acall(agent_fn, current)
            decision, score = self._normalize(await _acall(evaluate_fn, proposal))

            accepted = decision.verdict is Verdict.ALLOW and score < self.hallucination_threshold
            if accepted:
                return HealingOutcome(healed=True, attempts=attempt, result=proposal,
                                      feedbacks=feedbacks, final_context=current)

            fb = SelfHealingPromptFeedback.from_decision(tool, decision, score)
            if fb is None:   # ALLOW verdict but hallucination over threshold
                fb = SelfHealingPromptFeedback(
                    tool=tool, reason=f"hallucination score {score:.2f} exceeds threshold",
                    hallucination_score=score)
            feedbacks.append(fb)
            # Reformulate: the refusal reason joins the context window.
            current = f"{current}\n\n{fb.to_system_prompt_instruction()}".strip()
            logger.info("self-healing attempt %d/%d refused (%s); reformulating",
                        attempt, self.max_retries, fb.reason)
            if attempt < self.max_retries:
                await self._backoff(attempt)

        reason = feedbacks[-1].reason if feedbacks else "no proposal accepted"
        outcome = HealingOutcome(healed=False, attempts=attempts, escalated=True,
                                 feedbacks=feedbacks, final_context=current,
                                 final_reason=reason)
        if self.on_escalate is not None:
            try:
                await _acall(self.on_escalate, outcome)
            except Exception:
                logger.exception("self-healing on_escalate callback failed")
        return outcome

    async def _backoff(self, attempt: int) -> None:
        delay = min(self.base_delay * (self.backoff_factor ** (attempt - 1)), self.max_delay)
        if delay > 0:
            await self._sleep(delay)

    @staticmethod
    def _normalize(evaluation: Any) -> tuple[Decision, float]:
        if isinstance(evaluation, tuple):
            decision, score = evaluation[0], (evaluation[1] if len(evaluation) > 1 else 0.0)
            return decision, float(score or 0.0)
        return evaluation, 0.0


__all__ = ["SelfHealingPromptFeedback", "SelfHealingLoop", "HealingOutcome"]
