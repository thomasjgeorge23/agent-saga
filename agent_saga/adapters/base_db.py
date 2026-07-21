import abc
from typing import Any, Dict, Optional
from ..decorator import current_saga

class BaseDBAdapter(abc.ABC):
    """Abstract base class for database ORM/client adapters that auto-register LIFO
    compensating operations when executed within a saga scope."""

    @abc.abstractmethod
    async def insert(self, table: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Insert a row/document and register a compensating DELETE.
        Returns the inserted row data (including any auto-generated IDs/fields)."""
        pass

    @abc.abstractmethod
    async def update(self, table: str, data: Dict[str, Any], where: Dict[str, Any]) -> Dict[str, Any]:
        """Update rows/documents matching `where` with `data` and register a compensating UPDATE to restore their old values.
        Returns the updated row data."""
        pass

    @abc.abstractmethod
    async def delete(self, table: str, where: Dict[str, Any]) -> list[Dict[str, Any]]:
        """Delete rows/documents matching `where` and register compensating INSERTs to restore deleted records.
        Returns a list of the deleted records."""
        pass

    def _register_compensation(self, handler: callable, *args: Any, **kwargs: Any) -> None:
        """Helper to register a compensating action on the current active saga, if one exists."""
        saga_ctx = current_saga()
        if saga_ctx is not None:
            saga_ctx.compensate(handler, *args, **kwargs)
