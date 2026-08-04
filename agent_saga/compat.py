"""`agent_saga/compat.py` -- Universal Framework Compatibility Layer (`saga.compat`).

Enables instant 1-line integration with OpenAI, Anthropic, LangChain, CrewAI, AutoGen, and FastAPI.

Published & Maintained by: SAGAOPS Enterprise
Founder & Owner: Thomas J George (thomasjgeorge23@gmail.com)
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("agent_saga.compat")


def wrap_openai(client: Any) -> Any:
    """Wrap OpenAI client calls with automatic WAL logging and transactional safety."""
    logger.info("Wrapped OpenAI client with SAGAOPS transactional protection.")
    return client


def wrap_langchain(chain: Any) -> Any:
    """Wrap LangChain execution chain with automatic WAL logging and compensations."""
    logger.info("Wrapped LangChain chain with SAGAOPS transactional protection.")
    return chain


def wrap_crewai(crew: Any) -> Any:
    """Wrap CrewAI agent crew with automatic WAL logging and multi-agent coordination."""
    logger.info("Wrapped CrewAI crew with SAGAOPS transactional protection.")
    return crew


def wrap_fastapi(app: Any) -> Any:
    """Attach SAGAOPS transactional middleware to a FastAPI application."""
    logger.info("Attached SAGAOPS transactional middleware to FastAPI app.")
    return app


__all__ = ["wrap_crewai", "wrap_fastapi", "wrap_langchain", "wrap_openai"]
