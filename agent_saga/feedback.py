"""Self-healing LLM prompt feedback loop.

Connects MissionCriticalGate and HallucinationDetector refusal events directly
to agent retry loops. Formats gate rejection verdicts into structured system prompt
instructions so the model automatically self-corrects on retry attempts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .gate import Decision, Verdict


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


__all__ = ["SelfHealingPromptFeedback"]
