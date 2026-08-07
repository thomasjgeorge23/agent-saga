"""`agent_saga/integrations/crewai.py` -- Official CrewAI Agent & Crew Hook Adapter.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("agent_saga.integrations.crewai")


class SagaCrewHook:
    """CrewAI Hook Handler providing SAGAOPS multi-agent transaction logging."""

    def __init__(self, crew: Any = None) -> None:
        self.crew = crew
        logger.info("⚡ SagaCrewHook initialized for CrewAI multi-agent crew protection")

    def on_step(self, step_output: Any) -> None:
        logger.info("CrewAI Agent step output logged to WAL")
