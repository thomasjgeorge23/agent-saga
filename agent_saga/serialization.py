import datetime
import importlib
import json
import uuid
from typing import Any

class SagaJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder for agent-saga capable of serializing Pydantic models,
    datetime/date objects, UUIDs, and sets."""
    
    def default(self, obj: Any) -> Any:
        # Pydantic v2 support
        if hasattr(obj, "model_dump") and callable(obj.model_dump):
            return {
                "__type__": "pydantic",
                "__class__": f"{obj.__class__.__module__}.{obj.__class__.__name__}",
                "value": obj.model_dump()
            }
        # Pydantic v1 support
        elif hasattr(obj, "dict") and callable(obj.dict):
            return {
                "__type__": "pydantic",
                "__class__": f"{obj.__class__.__module__}.{obj.__class__.__name__}",
                "value": obj.dict()
            }
        elif isinstance(obj, datetime.datetime):
            return {
                "__type__": "datetime",
                "value": obj.isoformat()
            }
        elif isinstance(obj, datetime.date):
            return {
                "__type__": "date",
                "value": obj.isoformat()
            }
        elif isinstance(obj, uuid.UUID):
            return {
                "__type__": "uuid",
                "value": str(obj)
            }
        elif isinstance(obj, set):
            return {
                "__type__": "set",
                "value": list(obj)
            }
        elif isinstance(obj, type) and issubclass(obj, BaseException):
            return {
                "__type__": "exception_class",
                "value": f"{obj.__module__}.{obj.__name__}"
            }
        return super().default(obj)


_DISALLOWED_MODULE_PREFIXES = (
    "os", "sys", "subprocess", "shutil", "importlib", "builtins",
    "ctypes", "socket", "webbrowser", "tempfile", "signal", "threading", "multiprocessing"
)


def _is_safe_module(module_name: str) -> bool:
    top_level = module_name.split(".")[0]
    return top_level not in _DISALLOWED_MODULE_PREFIXES


def saga_object_hook(dct: dict) -> Any:
    """Object hook for decoding custom types back into original python objects."""
    if "__type__" in dct:
        t = dct["__type__"]
        if t == "datetime":
            return datetime.datetime.fromisoformat(dct["value"])
        elif t == "date":
            return datetime.date.fromisoformat(dct["value"])
        elif t == "uuid":
            return uuid.UUID(dct["value"])
        elif t == "set":
            return set(dct["value"])
        elif t == "exception_class":
            val = dct["value"]
            try:
                module_name, class_name = val.rsplit(".", 1)
                if module_name == "builtins":
                    import builtins
                    return getattr(builtins, class_name)
                if not _is_safe_module(module_name):
                    return Exception
                module = importlib.import_module(module_name)
                return getattr(module, class_name)
            except Exception:
                import builtins
                if hasattr(builtins, val):
                    return getattr(builtins, val)
                return Exception
        elif t == "pydantic":
            class_path = dct["__class__"]
            value = dct["value"]
            try:
                module_name, class_name = class_path.rsplit(".", 1)
                if not _is_safe_module(module_name):
                    return value
                module = importlib.import_module(module_name)
                cls = getattr(module, class_name)
                return cls(**value)
            except Exception:
                # If class cannot be resolved/imported, fall back to raw dict representation
                return value
    return dct


def dumps(obj: Any) -> str:
    """Serialize object to JSON string using SagaJSONEncoder."""
    return json.dumps(obj, cls=SagaJSONEncoder)


def loads(s: str) -> Any:
    """Deserialize JSON string using saga_object_hook."""
    return json.loads(s, object_hook=saga_object_hook)
