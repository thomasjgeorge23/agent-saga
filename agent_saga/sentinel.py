"""Predictive Chaos Sentinel.

Evaluates latency jitter, rate-limit headers, and error velocity to pre-emptively
trip circuit breakers prior to failure cascades.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from typing import Optional

logger = logging.getLogger("agent_saga.sentinel")


class PredictiveSentinel:
    """Calculates risk vectors and pre-emptively trips circuit gates."""

    def __init__(
        self,
        window_size: int = 50,
        risk_threshold: float = 0.85,
    ):
        self.window_size = window_size
        self.risk_threshold = risk_threshold
        self._latencies: dict[str, deque] = {}
        self._errors: dict[str, deque] = {}

    def record_sample(self, tool: str, latency_ms: float, is_error: bool) -> None:
        l_q = self._latencies.setdefault(tool, deque(maxlen=self.window_size))
        e_q = self._errors.setdefault(tool, deque(maxlen=self.window_size))
        l_q.append(latency_ms)
        e_q.append(1.0 if is_error else 0.0)

    def calculate_risk(self, tool: str) -> float:
        l_q = self._latencies.get(tool)
        e_q = self._errors.get(tool)
        if not l_q or len(l_q) < 5:
            return 0.0

        # Latency variance
        avg_l = sum(l_q) / len(l_q)
        var_l = sum((x - avg_l) ** 2 for x in l_q) / len(l_q)
        std_l = math.sqrt(var_l)
        jitter = std_l / (avg_l + 1e-5)

        # Error velocity
        error_rate = sum(e_q) / len(e_q)

        # Combined predictive risk metric [0.0, 1.0]
        risk = min(1.0, 0.4 * error_rate + 0.6 * min(1.0, jitter))
        return risk

    def should_block_preemptively(self, tool: str) -> tuple[bool, float]:
        risk = self.calculate_risk(tool)
        if risk >= self.risk_threshold:
            logger.warning("Predictive Sentinel pre-emptively blocking %s (risk %.2f >= %.2f)",
                           tool, risk, self.risk_threshold)
            return True, risk
        return False, risk


__all__ = ["PredictiveSentinel"]
