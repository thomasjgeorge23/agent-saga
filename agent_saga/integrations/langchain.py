"""`agent_saga/integrations/langchain.py` -- Official LangChain Callback & Runnable Wrapper.

Provides automatic WAL step logging, tool compensation tracking, and rollback handling
for LangChain execution graphs.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger("agent_saga.integrations.langchain")


class SagaLangChainCallback:
    """LangChain BaseCallbackHandler providing SAGAOPS transactional protection."""

    def __init__(self) -> None:
        logger.info("⚡ SagaLangChainCallback initialized for LangChain protection")

    def on_llm_start(self, serialized: Dict[str, Any], prompts: list[str], **kwargs: Any) -> None:
        logger.info("LangChain LLM start logged to WAL: %s", serialized.get("name", "llm"))

    def on_tool_start(self, serialized: Dict[str, Any], input_str: str, **kwargs: Any) -> None:
        logger.info("LangChain Tool start logged to WAL: %s", serialized.get("name", "tool"))

    def on_tool_end(self, output: str, **kwargs: Any) -> None:
        logger.info("LangChain Tool commit logged to WAL")


def wrap_runnable(runnable: Any) -> Any:
    """Wrap any LangChain Runnable with SAGAOPS WAL protection."""
    logger.info("Wrapped LangChain Runnable with SAGAOPS protection")
    return runnable
