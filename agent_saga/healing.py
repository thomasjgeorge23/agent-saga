"""Autonomous Graph Self-Healing Engine.

Discovers alternative execution paths, repairs broken parameters, and automatically
fails over to fallback tool definitions before triggering full saga rollbacks.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

logger = logging.getLogger("agent_saga.healing")


@dataclass
class HealingPath:
    primary_tool: str
    fallback_tool: str
    fallback_fn: Callable[..., Any]
    param_mapper: Optional[Callable[[dict, Exception], dict]] = None


@dataclass
class SelfHealedProposal:
    """Structure emitted when a saga automatically heals a failed step via adaptive fallbacks."""

    healed: bool
    primary_tool: str
    fallback_tool: str
    result: Any
    explanation: str


class SelfHealingGraph:
    """Topological graph repair engine for autonomous AI tool recovery."""

    def __init__(self, paths: Optional[Sequence[HealingPath]] = None):
        self._paths: dict[str, list[HealingPath]] = {}
        if paths:
            for path in paths:
                self.register_path(path)

    def register_path(self, path: HealingPath) -> None:
        self._paths.setdefault(path.primary_tool, []).append(path)

    async def try_heal_with_proposal(
        self,
        tool: str,
        kwargs: dict,
        error: Exception,
    ) -> Optional[SelfHealedProposal]:
        """Attempt self-healing and return a human-readable proposal for the AI."""
        success, result, fallback_tool = await self.try_heal(tool, kwargs, error)
        if not success:
            return None

        explanation = (
            f"Primary tool '{tool}' failed ({error}). Automatically self-healed by "
            f"switching execution to fallback tool '{fallback_tool}' successfully."
        )
        return SelfHealedProposal(
            healed=True,
            primary_tool=tool,
            fallback_tool=fallback_tool,
            result=result,
            explanation=explanation,
        )

    async def try_heal(
        self,
        tool: str,
        kwargs: dict,
        error: Exception,
    ) -> tuple[bool, Any, str]:
        """Attempt alternative execution paths for a failed tool call."""
        paths = self._paths.get(tool, [])
        if not paths:
            return False, None, ""

        for path in paths:
            logger.info("Attempting self-healing path %s -> %s due to: %r",
                        tool, path.fallback_tool, error)
            try:
                repair_kwargs = dict(kwargs)
                if path.param_mapper is not None:
                    repair_kwargs = path.param_mapper(repair_kwargs, error)

                if inspect.iscoroutinefunction(path.fallback_fn):
                    result = await path.fallback_fn(**repair_kwargs)
                else:
                    result = path.fallback_fn(**repair_kwargs)

                logger.info("Self-healing path %s -> %s succeeded", tool, path.fallback_tool)
                return True, result, path.fallback_tool
            except Exception as exc:
                logger.warning("Self-healing path %s -> %s failed: %r",
                               tool, path.fallback_tool, exc)

        return False, None, ""


__all__ = ["HealingPath", "SelfHealingGraph", "SelfHealedProposal"]
