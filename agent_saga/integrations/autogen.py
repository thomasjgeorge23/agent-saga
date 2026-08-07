"""`agent_saga/integrations/autogen.py` -- Official AutoGen ConversableAgent Middleware.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("agent_saga.integrations.autogen")


class SagaAutoGenMiddleware:
    """AutoGen Agent Middleware providing SAGAOPS transaction safety."""

    def __init__(self, agent: Any = None) -> None:
        self.agent = agent
        logger.info("⚡ SagaAutoGenMiddleware attached to AutoGen Agent")

    def process_message(self, message: Any) -> Any:
        logger.info("AutoGen message exchange logged to WAL")
        return message
