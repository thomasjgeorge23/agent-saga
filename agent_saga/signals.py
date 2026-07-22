"""Signal & Query Bus (Temporal Signals/Queries & Camunda Messages Parity).

Enables asynchronous steering/interrupting of active sagas (SignalBus) and
synchronous in-flight state queries (QueryBus).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger("agent_saga.signals")


@dataclass
class SignalMessage:
    saga_id: str
    signal_name: str
    payload: dict[str, Any]
    timestamp: float


class SignalBus:
    """Routes external asynchronous signals to running sagas."""

    def __init__(self):
        self._handlers: dict[str, dict[str, list[Callable]]] = {}  # saga_id -> signal_name -> callbacks

    def register_handler(self, saga_id: str, signal_name: str, callback: Callable[[SignalMessage], Any]) -> None:
        saga_handlers = self._handlers.setdefault(saga_id, {})
        callbacks = saga_handlers.setdefault(signal_name, [])
        callbacks.append(callback)

    async def send_signal(self, saga_id: str, signal_name: str, payload: Optional[dict] = None) -> SignalMessage:
        now = time.time()
        try:
            loop = asyncio.get_running_loop()
            now = loop.time()
        except RuntimeError:
            pass

        msg = SignalMessage(
            saga_id=saga_id,
            signal_name=signal_name,
            payload=payload or {},
            timestamp=now,
        )
        saga_handlers = self._handlers.get(saga_id, {})
        callbacks = saga_handlers.get(signal_name, [])
        for cb in callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(msg)
                else:
                    cb(msg)
            except Exception as exc:
                logger.error("Signal handler failed for saga %s (%s): %r", saga_id, signal_name, exc)
        return msg


class QueryBus:
    """Allows synchronous or asynchronous state queries on active sagas."""

    def __init__(self):
        self._query_handlers: dict[str, dict[str, Callable]] = {}  # saga_id -> query_name -> handler

    def register_query(self, saga_id: str, query_name: str, handler: Callable[[], Any]) -> None:
        self._query_handlers.setdefault(saga_id, {})[query_name] = handler

    async def query(self, saga_id: str, query_name: str) -> Any:
        handler = self._query_handlers.get(saga_id, {}).get(query_name)
        if not handler:
            raise KeyError(f"No handler registered for query {query_name!r} on saga {saga_id!r}")
        if asyncio.iscoroutinefunction(handler):
            return await handler()
        return handler()


_SIGNAL_BUS = SignalBus()
_QUERY_BUS = QueryBus()


def get_signal_bus() -> SignalBus:
    return _SIGNAL_BUS


def get_query_bus() -> QueryBus:
    return _QUERY_BUS


__all__ = ["SignalMessage", "SignalBus", "QueryBus", "get_signal_bus", "get_query_bus"]
