"""`agent_saga/pydantic.py` -- Native Pydantic v2 Schema Integrator.

Extracts Pydantic v2 model schemas for PreFlightGate validation, inverse argument mapping,
and automatic UI field compilation.

Published & Maintained by: SAGAOPS Enterprise
Founder & Owner: Thomas J George (thomasjgeorge23@gmail.com)
"""

from __future__ import annotations

from typing import Any, Dict, Type


class PydanticSagaAdapter:
    """Introspects Pydantic BaseModel classes for saga schema derivation."""

    @staticmethod
    def extract_schema(model_class: Type[Any]) -> Dict[str, Any]:
        """Extract Pydantic model json schema safely across v1 and v2."""
        if hasattr(model_class, "model_json_schema"):
            return model_class.model_json_schema()
        elif hasattr(model_class, "schema"):
            return model_class.schema()
        else:
            return {"type": "object", "properties": {}}

    @staticmethod
    def validate_kwargs(model_class: Type[Any], kwargs: Dict[str, Any]) -> Any:
        """Validate kwargs against Pydantic model."""
        if hasattr(model_class, "model_validate"):
            return model_class.model_validate(kwargs)
        elif hasattr(model_class, "parse_obj"):
            return model_class.parse_obj(kwargs)
        else:
            return kwargs


__all__ = ["PydanticSagaAdapter"]
