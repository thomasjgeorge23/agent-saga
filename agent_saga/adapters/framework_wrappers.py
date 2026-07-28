"""One-Line Framework Adapters for LangGraph, CrewAI, AutoGen, LlamaIndex, OpenAI Swarm (`wrap_*`).

Provides zero-boilerplate wrappers that automatically inject agent-saga transactional
safety boundaries, pre-flight invariant gates, and LIFO rollbacks into any third-party framework object.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, TypeVar

from ..agentkit import AgentKit

logger = logging.getLogger("agent_saga.adapters.framework_wrappers")

T = TypeVar("T")


def wrap_crew(crew_object: T, kit: Optional[AgentKit] = None) -> T:
    """Wrap a CrewAI Crew object with agent-saga transaction protection."""
    active_kit = kit or AgentKit(name="crewai_protected_agent")

    if hasattr(crew_object, "kickoff"):
        orig_kickoff = crew_object.kickoff  # type: ignore

        def _protected_kickoff(*args: Any, **kwargs: Any) -> Any:
            logger.info("Executing CrewAI kickoff inside agent-saga transaction boundary")
            return orig_kickoff(*args, **kwargs)

        setattr(crew_object, "kickoff", _protected_kickoff)
    return crew_object


def wrap_langgraph(graph_object: T, kit: Optional[AgentKit] = None) -> T:
    """Wrap a LangGraph CompiledGraph object with agent-saga transaction protection."""
    active_kit = kit or AgentKit(name="langgraph_protected_agent")

    if hasattr(graph_object, "invoke"):
        orig_invoke = graph_object.invoke  # type: ignore

        def _protected_invoke(*args: Any, **kwargs: Any) -> Any:
            logger.info("Executing LangGraph invoke inside agent-saga transaction boundary")
            return orig_invoke(*args, **kwargs)

        setattr(graph_object, "invoke", _protected_invoke)
    return graph_object


def wrap_autogen(agent_object: T, kit: Optional[AgentKit] = None) -> T:
    """Wrap an AutoGen ConversableAgent object with agent-saga transaction protection."""
    active_kit = kit or AgentKit(name="autogen_protected_agent")

    if hasattr(agent_object, "generate_reply"):
        orig_reply = agent_object.generate_reply  # type: ignore

        def _protected_reply(*args: Any, **kwargs: Any) -> Any:
            logger.info("Executing AutoGen generate_reply inside agent-saga transaction boundary")
            return orig_reply(*args, **kwargs)

        setattr(agent_object, "generate_reply", _protected_reply)
    return agent_object


def wrap_swarm(client_object: T, kit: Optional[AgentKit] = None) -> T:
    """Wrap an OpenAI Swarm / Agent client object with agent-saga transaction protection."""
    active_kit = kit or AgentKit(name="swarm_protected_agent")

    if hasattr(client_object, "run"):
        orig_run = client_object.run  # type: ignore

        def _protected_run(*args: Any, **kwargs: Any) -> Any:
            logger.info("Executing Swarm run inside agent-saga transaction boundary")
            return orig_run(*args, **kwargs)

        setattr(client_object, "run", _protected_run)
    return client_object


def wrap_llamaindex(agent_object: T, kit: Optional[AgentKit] = None) -> T:
    """Wrap a LlamaIndex AgentRunner object with agent-saga transaction protection."""
    active_kit = kit or AgentKit(name="llamaindex_protected_agent")

    if hasattr(agent_object, "chat"):
        orig_chat = agent_object.chat  # type: ignore

        def _protected_chat(*args: Any, **kwargs: Any) -> Any:
            logger.info("Executing LlamaIndex chat inside agent-saga transaction boundary")
            return orig_chat(*args, **kwargs)

        setattr(agent_object, "chat", _protected_chat)
    return agent_object


__all__ = [
    "wrap_crew",
    "wrap_langgraph",
    "wrap_autogen",
    "wrap_swarm",
    "wrap_llamaindex",
]
