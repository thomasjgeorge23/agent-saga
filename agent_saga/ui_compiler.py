"""`agent_saga/ui_compiler.py` -- Declarative Auto-UI Compiler for SAGAOPS.

Inspects Python @saga functions, AgentKit tools, and Pydantic/dataclass signatures
to compile machine-readable UI component schemas, form layouts, and React types.

Founder & Owner: Thomas J George (thomasjgeorge23@gmail.com)
Published & Maintained by: SAGAOPS Enterprise
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Callable, Dict, List, Optional, Type, get_type_hints


class UIFieldSchema:
    def __init__(self, name: str, field_type: str, required: bool = True, default: Any = None, description: str = ""):
        self.name = name
        self.field_type = field_type
        self.required = required
        self.default = default
        self.description = description

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "field_type": self.field_type,
            "required": self.required,
            "default": self.default,
            "description": self.description,
        }


class DeclarativeUISchema:
    def __init__(self, title: str, tool_name: str, fields: List[UIFieldSchema], semantics: str = "COMPENSABLE"):
        self.title = title
        self.tool_name = tool_name
        self.fields = fields
        self.semantics = semantics

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "tool_name": self.tool_name,
            "semantics": self.semantics,
            "fields": [f.to_dict() for f in self.fields],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def compile_saga_ui(func: Callable[..., Any], title: Optional[str] = None, semantics: str = "COMPENSABLE") -> DeclarativeUISchema:
    """Compile any Python function into a DeclarativeUISchema."""
    name = getattr(func, "__name__", "saga_tool")
    sig = inspect.signature(func)
    type_hints = get_type_hints(func) if hasattr(func, "__annotations__") else {}

    fields: List[UIFieldSchema] = []
    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls", "ctx"):
            continue

        raw_type = type_hints.get(param_name, str)
        type_str = getattr(raw_type, "__name__", str(raw_type))

        if "int" in type_str.lower():
            field_type = "number"
        elif "float" in type_str.lower():
            field_type = "number"
        elif "bool" in type_str.lower():
            field_type = "boolean"
        elif "dict" in type_str.lower():
            field_type = "json"
        else:
            field_type = "text"

        required = param.default is inspect.Parameter.empty
        default_val = None if required else param.default

        fields.append(
            UIFieldSchema(
                name=param_name,
                field_type=field_type,
                required=required,
                default=default_val,
                description=f"Parameter '{param_name}' for {name}",
            )
        )

    ui_title = title or name.replace("_", " ").title()
    return DeclarativeUISchema(title=ui_title, tool_name=name, fields=fields, semantics=semantics)


__all__ = ["UIFieldSchema", "DeclarativeUISchema", "compile_saga_ui"]
