"""Real-Time Cost & Token Budget Guardrails (`CostBudgetGate`).

Enforces strict financial dollar limits (e.g. max $5.00 spend per saga context)
and real-time token quotas. Emits UI confirmation requests before budget exhaustion.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger("agent_saga.cost_gate")


class CostBudgetExceeded(Exception):
    """Raised when saga financial spend or token quota exceeds declared limits."""

    pass


@dataclass
class CostBudgetConfig:
    """Configuration for saga financial spend and token budget guardrails."""

    max_cost_usd: float = 5.00
    max_total_tokens: int = 100_000
    warning_threshold_pct: float = 0.80  # 80% triggers human confirmation request


class CostBudgetGate:
    """Real-time financial and token quota enforcement gate."""

    def __init__(self, config: Optional[CostBudgetConfig] = None):
        self.config = config or CostBudgetConfig()
        self.current_cost_usd: float = 0.0
        self.current_tokens: int = 0

    def record_usage(self, prompt_tokens: int, completion_tokens: int, cost_usd: float = 0.0) -> Dict[str, Any]:
        """Record model token usage and calculate cumulative financial spend."""
        tokens_added = prompt_tokens + completion_tokens
        self.current_tokens += tokens_added
        self.current_cost_usd += cost_usd

        # Check hard budget limits
        if self.current_cost_usd > self.config.max_cost_usd:
            raise CostBudgetExceeded(
                f"Saga financial spend exceeded limit: ${self.current_cost_usd:.4f} > ${self.config.max_cost_usd:.2f}"
            )

        if self.current_tokens > self.config.max_total_tokens:
            raise CostBudgetExceeded(
                f"Saga token quota exceeded limit: {self.current_tokens} > {self.config.max_total_tokens}"
            )

        requires_warning = (
            self.current_cost_usd >= (self.config.max_cost_usd * self.config.warning_threshold_pct)
        ) or (self.current_tokens >= (self.config.max_total_tokens * self.config.warning_threshold_pct))

        return {
            "status": "OK",
            "current_cost_usd": self.current_cost_usd,
            "current_tokens": self.current_tokens,
            "requires_operator_approval": requires_warning,
        }


__all__ = ["CostBudgetGate", "CostBudgetConfig", "CostBudgetExceeded"]
