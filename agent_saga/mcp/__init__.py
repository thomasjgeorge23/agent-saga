"""Saga-aware MCP proxy.

Gives an unmodified agent transactional boundaries, a policy gate, spend limits
and a tamper-evident audit trail, by sitting between it and its MCP servers.

    from agent_saga.mcp import SagaMCPProxy, load_policy_file

Zero dependencies: the proxy speaks JSON-RPC directly rather than through an
MCP SDK, so it works against any server regardless of which SDK version that
server was written with.
"""

from .policy import (
    CompensationSpec,
    PolicyError,
    ProxyPolicy,
    ToolPolicy,
    extract,
    load_policy,
    load_policy_file,
    skeleton_from_observations,
)
from .proxy import Dispatcher, SagaMCPProxy, ToolNotDeclared, set_mcp_dispatcher

__all__ = [
    "SagaMCPProxy", "ToolNotDeclared", "set_mcp_dispatcher", "Dispatcher",
    "ProxyPolicy", "ToolPolicy", "CompensationSpec", "PolicyError",
    "load_policy", "load_policy_file", "extract", "skeleton_from_observations",
]
