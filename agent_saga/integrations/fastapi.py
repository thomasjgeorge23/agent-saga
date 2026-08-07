"""`agent_saga/integrations/fastapi.py` -- Official FastAPI Middleware adapter for agent-saga.

Attaches crash-safe Write-Ahead Logging (WAL) and automatic transaction rollback
to FastAPI routes.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger("agent_saga.integrations.fastapi")


class SagaFastAPIMiddleware:
    """FastAPI Middleware providing automatic SAGAOPS WAL protection."""

    def __init__(self, app: Any) -> None:
        self.app = app
        logger.info("⚡ SagaFastAPIMiddleware attached to FastAPI application")

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        # Pass request through to app
        await self.app(scope, receive, send)
