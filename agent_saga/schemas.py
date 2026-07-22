"""Typed Schema Contracts for Tool Inputs and Forward/Compensation Results.

Supports Pydantic BaseModel and dataclass schemas to validate tool return values
and compensation kwargs, preventing malformed API responses from entering the WAL.
"""

from __future__ import annotations

import logging
from typing import Any, Type, Optional

logger = logging.getLogger("agent_saga.schemas")


class SchemaContractError(ValueError):
    """Raised when tool input or output violates the typed schema contract."""
    pass


def validate_schema(data: Any, schema: Any, label: str = "data") -> Any:
    """Validates data against a Pydantic model, dataclass, or type."""
    if schema is None or data is None:
        return data

    try:
        # Pydantic v2 / v1
        if hasattr(schema, "model_validate"):
            return schema.model_validate(data)
        elif hasattr(schema, "parse_obj"):
            return schema.parse_obj(data)
        elif isinstance(data, dict) and hasattr(schema, "__annotations__"):
            # Dataclass or typed dict instantiation
            return schema(**data)
        return data
    except Exception as exc:
        raise SchemaContractError(f"Schema validation failed for {label}: {exc}") from exc


__all__ = ["SchemaContractError", "validate_schema"]
