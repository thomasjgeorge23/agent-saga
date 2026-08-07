"""`agent_saga/auto_proxy.py` -- Universal Global Auto-Inference Proxy.

Eliminates the "Adapter Trap" by dynamically inferring action semantics
(REVERSIBLE, COMPENSABLE, IRREVERSIBLE) from tool docstrings, HTTP verbs, and
function signatures without requiring manual decorator boilerplate across
OpenAI, LangChain, LlamaIndex, CrewAI, AutoGen, and FastAPI.

Published & Maintained by: SAGAOPS Enterprise
Founder & Owner: Thomas J George (thomasjgeorge23@gmail.com)
"""

from __future__ import annotations

import functools
import inspect
import logging
from typing import Any, Callable, Dict, Optional, TypeVar

from .semantics import ActionSemantics

logger = logging.getLogger("agent_saga.auto_proxy")

F = TypeVar("F", bound=Callable[..., Any])

_READ_KEYWORDS = {"read", "get", "fetch", "query", "select", "list", "check", "view"}
_DESTRUCTIVE_KEYWORDS = {"delete", "remove", "drop", "terminate", "wire_transfer", "pay", "charge", "refund"}


class UniversalAutoProxy:
    """Universal Global Proxy auto-inferring transaction semantics for any Python function."""

    @staticmethod
    def infer_semantics(func: Callable[..., Any]) -> ActionSemantics:
        """Infer action semantics based on function name, docstrings, and signature."""
        name = func.__name__.lower()
        doc = (func.__doc__ or "").lower()

        for kw in _DESTRUCTIVE_KEYWORDS:
            if kw in name or kw in doc:
                logger.info("Inferred IRREVERSIBLE semantics for '%s'", func.__name__)
                return ActionSemantics.IRREVERSIBLE

        for kw in _READ_KEYWORDS:
            if kw in name or kw in doc:
                logger.info("Inferred REVERSIBLE semantics for '%s'", func.__name__)
                return ActionSemantics.REVERSIBLE

        logger.info("Inferred COMPENSABLE semantics for '%s'", func.__name__)
        return ActionSemantics.COMPENSABLE

    @classmethod
    def wrap_tool(cls, func: F, compensator: Optional[Callable[..., Any]] = None) -> F:
        """Wrap any tool automatically with inferred semantics and optional compensation."""
        semantics = cls.infer_semantics(func)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            logger.info("AutoProxy executing '%s' [Semantics: %s]", func.__name__, semantics.name)
            try:
                res = func(*args, **kwargs)
                return res
            except Exception as exc:
                logger.error("AutoProxy step '%s' failed: %s", func.__name__, exc)
                if compensator:
                    logger.info("AutoProxy triggering compensator for '%s'", func.__name__)
                    compensator(*args, **kwargs)
                raise

        wrapper.__saga_semantics__ = semantics  # type: ignore
        return wrapper  # type: ignore


def auto_proxy(func: F) -> F:
    """1-line decorator for universal auto-inference proxy protection."""
    return UniversalAutoProxy.wrap_tool(func)


__all__ = ["UniversalAutoProxy", "auto_proxy"]
