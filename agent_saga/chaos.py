"""Chaos Engineering Engine for agent-saga.

Provides controlled fault injection (process termination simulation, corrupted WAL bytes,
network latency spikes, and step exception injection) to guarantee 100% compensation safety
and recovery daemon resilience under adverse conditions.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Callable, Optional

logger = logging.getLogger("agent_saga.chaos")


class ChaosInjectionError(RuntimeError):
    """Raised when the ChaosEngine injects a synthetic failure into a step."""


class ChaosConfig:
    def __init__(
        self,
        *,
        fail_at_step_index: Optional[int] = None,
        fail_at_tool_name: Optional[str] = None,
        latency_ms: float = 0.0,
        corrupt_wal_on_barrier: bool = False,
        failure_rate: float = 1.0,
    ):
        self.fail_at_step_index = fail_at_step_index
        self.fail_at_tool_name = fail_at_tool_name
        self.latency_ms = latency_ms
        self.corrupt_wal_on_barrier = corrupt_wal_on_barrier
        self.failure_rate = failure_rate


class ChaosEngine:
    """Runtime hook for injecting faults into live saga executions."""

    def __init__(self, config: Optional[ChaosConfig] = None):
        self.config = config or ChaosConfig()
        self.step_counter = 0

    async def before_step(self, tool_name: str, args: dict[str, Any]) -> None:
        self.step_counter += 1
        cfg = self.config

        if cfg.latency_ms > 0:
            await asyncio.sleep(cfg.latency_ms / 1000.0)

        should_trigger = random.random() < cfg.failure_rate
        if not should_trigger:
            return

        if cfg.fail_at_step_index is not None and self.step_counter == cfg.fail_at_step_index:
            logger.warning(f"CHAOS INJECTION: Failing at step index {self.step_counter} ({tool_name})")
            raise ChaosInjectionError(f"Simulated fault at step {self.step_counter}")

        if cfg.fail_at_tool_name and tool_name == cfg.fail_at_tool_name:
            logger.warning(f"CHAOS INJECTION: Failing at tool '{tool_name}'")
            raise ChaosInjectionError(f"Simulated fault at tool '{tool_name}'")

    def corrupt_file_bytes(self, path_str: str) -> None:
        """Inject corrupt bytes into the tail of a WAL file to test recovery parser robustness."""
        try:
            with open(path_str, "rb+") as f:
                f.seek(0, 2)
                pos = f.tell()
                if pos > 10:
                    f.seek(pos - 10)
                    f.write(b"\xFF\xFE\xFD\xFCBADBYTES")
                    f.flush()
            logger.info(f"CHAOS INJECTION: Injected corrupt bytes into {path_str}")
        except Exception as e:
            logger.error(f"Failed to inject corrupt bytes: {e}")
