"""`agent_saga/mcp/transaction_proxy.py` -- Native MCP Transaction Proxy Engine.

Published & Maintained by: SAGAOPS Enterprise
Founder & Owner: Thomas J George (thomasjgeorge23@gmail.com)
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional
from agent_saga.decorator import saga_scope
from agent_saga.gate import PreFlightGate, Verdict
from agent_saga.semantics import ActionSemantics

logger = logging.getLogger("agent_saga.mcp.transaction_proxy")


class MCPToolDefinition:
    def __init__(self, name: str, description: str, parameters_schema: Dict[str, Any], semantics: str = "COMPENSABLE"):
        self.name = name
        self.description = description
        self.parameters_schema = parameters_schema
        self.semantics = semantics

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.parameters_schema,
            "saga_semantics": self.semantics,
        }


class MCPTransactionProxy:
    """Native MCP (Model Context Protocol) Transaction Proxy Engine."""

    def __init__(self, server_name: str = "default_mcp_server", gate: Optional[PreFlightGate] = None):
        self.server_name = server_name
        self.gate = gate
        self.tools: Dict[str, MCPToolDefinition] = {}
        self.handlers: Dict[str, Callable[..., Any]] = {}
        self.inverses: Dict[str, Callable[..., Any]] = {}

    def register_tool(
        self,
        name: str,
        handler: Callable[..., Any],
        inverse_handler: Optional[Callable[..., Any]] = None,
        semantics: str = "COMPENSABLE",
        description: str = "",
        parameters_schema: Optional[Dict[str, Any]] = None,
    ):
        """Register an MCP tool with explicit saga semantics and inverse handler."""
        schema = parameters_schema or {"type": "object", "properties": {}}
        tool_def = MCPToolDefinition(name, description, schema, semantics)
        self.tools[name] = tool_def
        self.handlers[name] = handler
        if inverse_handler:
            self.inverses[name] = inverse_handler

    async def execute_mcp_call(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute an MCP tool call inside an isolated agent-saga transaction boundary."""
        if tool_name not in self.handlers:
            raise KeyError(f"MCP tool '{tool_name}' not registered on server '{self.server_name}'")

        tool_def = self.tools[tool_name]
        handler = self.handlers[tool_name]
        inverse = self.inverses.get(tool_name)

        if self.gate:
            decision = self.gate.check(tool_name, arguments)
            if decision.verdict == Verdict.BLOCK:
                raise PermissionError(f"PreFlightGate BLOCKED MCP tool '{tool_name}': {decision.reason}")

        async with saga_scope(name=f"mcp_{self.server_name}_{tool_name}") as saga:
            semantics_enum = (
                ActionSemantics.COMPENSABLE
                if tool_def.semantics == "COMPENSABLE"
                else ActionSemantics.REVERSIBLE
                if tool_def.semantics == "REVERSIBLE"
                else ActionSemantics.IRREVERSIBLE
            )

            result = await saga.execute(
                tool_name,
                semantics_enum,
                handler,
                forward_kwargs=arguments,
                compensate=inverse,
            )

            return {
                "status": "success",
                "tool": tool_name,
                "result": result,
                "saga_id": saga.saga_id,
            }

    def list_mcp_tools(self) -> List[Dict[str, Any]]:
        return [tool.to_dict() for tool in self.tools.values()]


__all__ = ["MCPToolDefinition", "MCPTransactionProxy"]
