"""One-line framework wrappers: a real saga boundary around a framework call.

`wrap_crew(crew)`, `wrap_langgraph(graph)`, and friends replace one method on
the object so the call runs inside a saga transaction. If it raises, every
compensable step registered during it is unwound LIFO before the exception
reaches the caller.

**What the boundary can and cannot do**, stated plainly because the previous
version of this module got it wrong and said otherwise:

  * It compensates the steps that were *registered* with agent-saga during the
    call -- tools wrapped with `kit.safe_tool`, or steps run through
    `ctx.execute` / the framework adapters in this package.
  * It cannot invent compensations for tools it never saw. A CrewAI crew whose
    tools are plain functions has nothing to roll back, and wrapping it does
    not change that. The boundary gives you the transaction, the WAL record,
    and the gate; the tools still have to declare their own semantics.

An earlier release of these wrappers logged "inside agent-saga transaction
boundary" and then called the original method unchanged -- no transaction, no
gate, no rollback. That is the same defect as `patch_all()` reporting SDKs it
had not patched, and the same rule applies: a safety layer that overstates its
coverage is worse than none, because the user stops looking for the real
protection. The wrappers now open the boundary they name, and
`is_saga_wrapped()` lets a caller verify it rather than trust a log line.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, Tuple, TypeVar

from ..agentkit import AgentKit

logger = logging.getLogger("agent_saga.adapters.framework_wrappers")

T = TypeVar("T")

__all__ = [
    "is_saga_wrapped",
    "wrap_autogen",
    "wrap_crew",
    "wrap_langgraph",
    "wrap_llamaindex",
    "wrap_method",
    "wrap_swarm",
]

_MARKER = "__agent_saga_wrapped__"


def is_saga_wrapped(obj: Any, method: str) -> bool:
    """True only if `obj.method` really does run inside a saga boundary.

    Exists so the claim is checkable. `patch_all()` learned this the expensive
    way: a report nobody can verify is indistinguishable from one that is wrong.
    """
    return bool(getattr(getattr(obj, method, None), _MARKER, False))


def wrap_method(obj: T, method: str, *, kit: Optional[AgentKit] = None,
                name: Optional[str] = None) -> T:
    """Replace `obj.method` with a version that runs inside a saga boundary.

    Sync and async methods are both supported. A sync method invoked from
    inside a running event loop raises rather than silently skipping the
    boundary -- blocking a running loop is impossible, not merely discouraged,
    and `enhance()` refuses in the same place for the same reason.

    Idempotent: wrapping twice leaves one boundary, not two nested ones.
    """
    original = getattr(obj, method, None)
    if original is None or not callable(original):
        raise AttributeError(
            f"{type(obj).__name__} has no callable {method!r} to wrap; refusing "
            f"to report protection for a method that does not exist")
    if getattr(original, _MARKER, False):
        return obj

    active = kit or AgentKit(name=name or f"{type(obj).__name__}_saga")

    if asyncio.iscoroutinefunction(original):
        async def protected(*args: Any, **kwargs: Any) -> Any:
            async with active.transaction():
                return await original(*args, **kwargs)
    else:
        def protected(*args: Any, **kwargs: Any) -> Any:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise RuntimeError(
                    f"{type(obj).__name__}.{method}() is synchronous and was "
                    f"called from inside a running event loop, where the saga "
                    f"boundary cannot block. Wrap the framework's async entry "
                    f"point instead, or call this from synchronous code.")

            async def _run() -> Any:
                async with active.transaction():
                    return original(*args, **kwargs)

            return asyncio.run(_run())

    protected.__name__ = getattr(original, "__name__", method)
    protected.__doc__ = getattr(original, "__doc__", None)
    setattr(protected, _MARKER, True)
    setattr(protected, "__agent_saga_original__", original)
    setattr(obj, method, protected)
    logger.info("agent-saga: %s.%s now runs inside a saga boundary",
                type(obj).__name__, method)
    return obj


def _wrap_first_available(obj: T, methods: Tuple[str, ...],
                          kit: Optional[AgentKit], label: str) -> T:
    """Wrap the first method the object actually exposes.

    Raises when none is present. The previous version returned the object
    untouched in that case, so a typo or a framework version bump produced an
    unprotected object that looked protected.
    """
    for method in methods:
        if callable(getattr(obj, method, None)):
            return wrap_method(obj, method, kit=kit, name=label)
    raise AttributeError(
        f"{type(obj).__name__} exposes none of {list(methods)}, so there is "
        f"nothing to wrap. Name the method explicitly with "
        f"wrap_method(obj, '<method>') rather than accept an object that is "
        f"not protected.")


def wrap_crew(crew_object: T, kit: Optional[AgentKit] = None) -> T:
    """Run a CrewAI `Crew.kickoff()` inside a saga boundary."""
    return _wrap_first_available(crew_object, ("kickoff_async", "kickoff"),
                                 kit, "crewai_saga")


def wrap_langgraph(graph_object: T, kit: Optional[AgentKit] = None) -> T:
    """Run a compiled LangGraph's `ainvoke`/`invoke` inside a saga boundary."""
    return _wrap_first_available(graph_object, ("ainvoke", "invoke"),
                                 kit, "langgraph_saga")


def wrap_autogen(agent_object: T, kit: Optional[AgentKit] = None) -> T:
    """Run an AutoGen agent's reply generation inside a saga boundary."""
    return _wrap_first_available(agent_object, ("a_generate_reply", "generate_reply"),
                                 kit, "autogen_saga")


def wrap_swarm(client_object: T, kit: Optional[AgentKit] = None) -> T:
    """Run an OpenAI Swarm/Agents client `run` inside a saga boundary."""
    return _wrap_first_available(client_object, ("arun", "run"), kit, "swarm_saga")


def wrap_llamaindex(agent_object: T, kit: Optional[AgentKit] = None) -> T:
    """Run a LlamaIndex agent's `achat`/`chat` inside a saga boundary."""
    return _wrap_first_available(agent_object, ("achat", "chat"), kit,
                                 "llamaindex_saga")
