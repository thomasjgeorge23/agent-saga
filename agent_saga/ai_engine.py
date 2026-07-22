"""AI Engine Synergy & Failure-Proof Agent Integration Module.

Solves the core pain points AI model engines encounter during coding and tool execution:
1. Semantic Output Verification (detecting soft errors and schema drift in tool returns)
2. Context Sanitization & Pruning (preventing failed retry pollution in LLM context windows)
3. Loop Entropy & Infinite Cycle Detection (stopping repetitive tool call token drain)
4. Universal Provider Tool Parameter Normalization (ensuring seamless Multi-Model handoffs)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

logger = logging.getLogger("agent_saga.ai_engine")


@dataclass
class VerifiedOutput:
    is_valid: bool
    data: Any
    reason: str


class SemanticOutputVerifier:
    """Validates tool execution return values against expected schemas and semantic assertions."""

    def __init__(self, validators: Optional[Sequence[Callable[[Any], tuple[bool, str]]]] = None):
        self.validators = list(validators or [])

    def verify(self, output: Any) -> VerifiedOutput:
        if output is None:
            return VerifiedOutput(False, None, "Tool returned None output")

        # Detect common soft error dictionaries (e.g. {"error": "..."})
        if isinstance(output, dict):
            if "error" in output and output["error"]:
                return VerifiedOutput(False, output, f"Soft error in tool output: {output['error']}")
            if output.get("status") in ("failed", "error"):
                return VerifiedOutput(False, output, f"Failed status in tool output: {output.get('message', 'unknown')}")

        for v in self.validators:
            try:
                ok, msg = v(output)
                if not ok:
                    return VerifiedOutput(False, output, f"Semantic validator failed: {msg}")
            except Exception as exc:
                return VerifiedOutput(False, output, f"Semantic validator raised error: {exc!r}")

        return VerifiedOutput(True, output, "Verified valid")


class ContextSanitizer:
    """Filters out failed/aborted saga trial steps to maintain clean LLM context windows."""

    @staticmethod
    def prune_failed_trials(history: list[dict]) -> list[dict]:
        """Keep only committed or successfully compensated steps in prompt history."""
        clean_history = []
        for event in history:
            event_type = event.get("type") or event.get("event")
            # Exclude raw UNKNOWN or failed attempts from contaminating context
            if event_type in ("STEP_COMMITTED", "COMPLETED_VIA_FALLBACK", "COMPENSATED"):
                clean_history.append(event)
        return clean_history


class LoopEntropyDetector:
    """Detects repetitive AI tool call patterns and parameter thrashing to save tokens."""

    def __init__(self, max_repetition: int = 3):
        self.max_repetition = max_repetition
        self._history: list[tuple[str, str]] = []

    def check_call(self, tool: str, kwargs_str: str) -> tuple[bool, str]:
        self._history.append((tool, kwargs_str))
        if len(self._history) < self.max_repetition:
            return False, "Normal"

        recent = self._history[-self.max_repetition:]
        # Check if identical tool and parameters repeated N times
        if len(set(recent)) == 1:
            return True, f"Identical call to '{tool}' repeated {self.max_repetition} times sequentially"

        return False, "Normal"


class UniversalToolAdapter:
    """Normalizes tool parameters across OpenAI, Anthropic, Gemini, and local LLM formats."""

    @staticmethod
    def normalize_args(raw_kwargs: dict) -> dict:
        normalized = dict(raw_kwargs)
        # Unwrap nested JSON string parameters if an model serialized them
        for k, v in list(normalized.items()):
            if isinstance(v, str) and (v.startswith("{") or v.startswith("[")):
                try:
                    import json
                    parsed = json.loads(v)
                    if isinstance(parsed, (dict, list)):
                        normalized[k] = parsed
                except Exception:
                    pass
        return normalized


__all__ = [
    "SemanticOutputVerifier",
    "VerifiedOutput",
    "ContextSanitizer",
    "LoopEntropyDetector",
    "UniversalToolAdapter",
]
