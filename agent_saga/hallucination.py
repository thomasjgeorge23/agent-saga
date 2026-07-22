"""Anti-Hallucination Reality Anchor & Self-Correcting Callback Loop.

Prevents AI agents from performing actions with hallucinated parameters, un-grounded
assumptions, or broken schema payloads by executing pre-flight state verification
and automatic self-correcting prompt/payload correction loops.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence, Union

logger = logging.getLogger("agent_saga.hallucination")


class HallucinationDetected(Exception):
    """Raised when an AI payload or tool call violates factual reality anchors."""

    def __init__(self, reason: str, payload: Any, feedback: str):
        self.reason = reason
        self.payload = payload
        self.feedback = feedback
        super().__init__(f"Hallucination detected: {reason}. Feedback: {feedback}")


@dataclass
class GroundingFact:
    key: str
    expected_value: Any
    validator: Optional[Callable[[Any], bool]] = None


class RealityAnchor:
    """Validates candidate payloads against immutable facts and schema invariants."""

    def __init__(
        self,
        facts: Optional[Sequence[GroundingFact]] = None,
        validators: Optional[Sequence[Callable[[dict], bool]]] = None,
    ):
        self.facts = list(facts or [])
        self.validators = list(validators or [])

    def verify(self, payload: dict) -> tuple[bool, str]:
        """Check payload for factual accuracy and schema grounding."""
        if not isinstance(payload, dict):
            return False, f"Expected dict payload, got {type(payload).__name__}"

        for fact in self.facts:
            if fact.key not in payload:
                return False, f"Missing required grounded key {fact.key!r} in payload"
            val = payload[fact.key]
            if fact.validator is not None:
                if not fact.validator(val):
                    return False, f"Value for key {fact.key!r} ({val!r}) failed reality anchor validation"
            elif val != fact.expected_value:
                return False, f"Value for key {fact.key!r} ({val!r}) contradicts ground truth ({fact.expected_value!r})"

        for validator in self.validators:
            try:
                if not validator(payload):
                    return False, "Custom reality anchor validator returned False"
            except Exception as exc:
                return False, f"Reality anchor validator raised error: {exc!r}"

        return True, "Grounded"


class SelfCorrectingLoop:
    """Wraps agent tool invocation with an automatic self-correcting retry callback."""

    def __init__(
        self,
        anchor: RealityAnchor,
        max_retries: int = 3,
        corrector: Optional[Callable[[dict, str], Union[dict, Any]]] = None,
    ):
        self.anchor = anchor
        self.max_retries = max_retries
        self.corrector = corrector

    async def execute_grounded(
        self,
        func: Callable[..., Any],
        payload: dict,
        *args,
        **kwargs,
    ) -> Any:
        current_payload = dict(payload)

        for attempt in range(1, self.max_retries + 1):
            is_valid, feedback = self.anchor.verify(current_payload)
            if is_valid:
                logger.info("Payload grounded successfully on attempt %d", attempt)
                if inspect.iscoroutinefunction(func):
                    return await func(current_payload, *args, **kwargs)
                return func(current_payload, *args, **kwargs)

            logger.warning(
                "Hallucination / drift detected on attempt %d/%d: %s",
                attempt, self.max_retries, feedback,
            )

            if attempt == self.max_retries:
                raise HallucinationDetected(
                    reason="Exhausted self-correction retries without reaching reality grounding",
                    payload=current_payload,
                    feedback=feedback,
                )

            if self.corrector is not None:
                try:
                    corrected = self.corrector(current_payload, feedback)
                    if inspect.isawaitable(corrected):
                        corrected = await corrected
                    if isinstance(corrected, dict):
                        current_payload = corrected
                except Exception as exc:
                    logger.error("Self-correction callback failed: %r", exc)

        raise HallucinationDetected("Self-correction loop failed", current_payload, "Unknown")


__all__ = [
    "RealityAnchor",
    "GroundingFact",
    "SelfCorrectingLoop",
    "HallucinationDetected",
]
