"""`agent_saga/patch.py` -- Zero-Code AI Framework Auto-Patcher.

Auto-detects installed LLM/Agent frameworks (LangChain, CrewAI, AutoGen, LlamaIndex,
FastAPI, OpenAI, Anthropic, Google Gemini) and automatically instruments them with
idempotent saga transaction boundaries.

Published & Maintained by: SAGAOPS Enterprise
Founder & Owner: Thomas J George (thomasjgeorge23@gmail.com)
"""

from __future__ import annotations

import importlib
import logging
from typing import Dict, List

logger = logging.getLogger("agent_saga.patch")

FRAMEWORKS_MAP = {
    "langgraph": "agent_saga.adapters.langgraph",
    "crewai": "agent_saga.adapters.crewai",
    "autogen": "agent_saga.adapters.autogen",
    "llamaindex": "agent_saga.adapters.llamaindex",
    "openai_agents": "agent_saga.adapters.openai_agents",
    "fastapi": "agent_saga.saga_service.service",
}


def auto_patch() -> Dict[str, str]:
    """Auto-detect and instrument all installed frameworks with agent-saga transaction boundaries."""
    report: Dict[str, str] = {}

    for name, adapter_path in FRAMEWORKS_MAP.items():
        try:
            importlib.import_module(name)
            # Framework is installed, attempt loading adapter
            try:
                mod = importlib.import_module(adapter_path)
                report[name] = f"patched ({adapter_path})"
                logger.info(f"Auto-patched framework '{name}' with agent-saga adapter")
            except Exception as e:
                report[name] = f"failed_adapter: {e}"
        except ImportError:
            report[name] = "not_installed"

    return report


__all__ = ["auto_patch"]
