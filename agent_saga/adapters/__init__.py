"""Framework adapters.

Each adapter makes agent-saga transparent to an existing agent framework: you
keep writing tools and graphs the framework's way, and wrapping them routes
every tool execution through a saga boundary.

Adapters import their framework lazily, inside functions -- importing this
package pulls in nothing. `from agent_saga.adapters.langgraph import wrap_tool`.
"""

__all__: list[str] = []
