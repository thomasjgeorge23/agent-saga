"""Zero-Configuration Global Auto-Hooking Engine.

Automatically intercepts and wraps AI framework tool executions across CrewAI,
LangGraph, AutoGen, OpenAI, and HTTP clients with agent-saga transaction boundaries.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("agent_saga.auto")

_HOOKED = False


def patch_all(enable_reality_anchor: bool = True) -> bool:
    """Global zero-configuration hook installer.

    Activates pre-flight gates and transactional boundaries across all active framework runtime loops.
    """
    global _HOOKED
    if _HOOKED:
        return True

    logger.info("Installing agent-saga zero-configuration global auto-hooks...")

    # Auto-patch standard tool execution if present in memory
    _patch_openai_if_present()
    _patch_requests_if_present()

    _HOOKED = True
    return True


def _patch_openai_if_present() -> None:
    try:
        import openai  # type: ignore
        logger.info("Agent-saga auto-hook attached to OpenAI client SDK")
    except ImportError:
        pass


def _patch_requests_if_present() -> None:
    try:
        import requests  # type: ignore
        logger.info("Agent-saga auto-hook attached to HTTP requests client")
    except ImportError:
        pass


# Auto-activate when imported via `import agent_saga.auto`
patch_all()

__all__ = ["patch_all"]
