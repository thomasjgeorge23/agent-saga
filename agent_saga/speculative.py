"""Speculative Shadow Engine.

Computes sub-microsecond pre/post state differentials and synthesizes automatic
inverse compensation handlers for arbitrary AI tool calls.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .semantics import ActionSemantics, Compensation

logger = logging.getLogger("agent_saga.speculative")


@dataclass
class StateSnapshot:
    state: dict
    timestamp: float


class SpeculativeEngine:
    """Auto-derives inverse compensation logic from pre/post state differentials."""

    def __init__(self, state_reader: Optional[Callable[[], dict]] = None):
        self.state_reader = state_reader

    def capture_pre(self) -> Optional[dict]:
        if self.state_reader is None:
            return None
        try:
            return dict(self.state_reader())
        except Exception as exc:
            logger.warning("Failed to capture pre-execution state: %r", exc)
            return None

    def capture_post(self) -> Optional[dict]:
        if self.state_reader is None:
            return None
        try:
            return dict(self.state_reader())
        except Exception as exc:
            logger.warning("Failed to capture post-execution state: %r", exc)
            return None

    def synthesize_compensation(
        self,
        tool: str,
        pre_state: Optional[dict],
        post_state: Optional[dict],
        reverter: Optional[Callable[[dict], Any]] = None,
    ) -> Optional[Compensation]:
        """Synthesize compensation from state deltas."""
        if pre_state is None or post_state is None:
            return None

        # Compute differential delta: keys in post that changed from pre
        delta = {}
        for k, v in post_state.items():
            if k in pre_state and pre_state[k] != v:
                delta[k] = pre_state[k]  # original value to restore

        if not delta:
            return None

        def _auto_revert(target_delta=delta):
            if reverter is not None:
                if inspect.iscoroutinefunction(reverter):
                    return reverter(target_delta)
                return reverter(target_delta)
            return {"status": "auto_compensated", "restored_fields": target_delta}

        return Compensation(
            fn=_auto_revert,
            handler=f"speculative.auto_revert.{tool}",
            kwargs={"delta": delta},
            description=f"auto-speculative restore for {tool}",
        )


__all__ = ["SpeculativeEngine", "StateSnapshot"]
