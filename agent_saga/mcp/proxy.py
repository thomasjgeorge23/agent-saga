"""Saga-aware MCP proxy: transactional boundaries with no change to the agent.

The library asks the agent's author to wrap tools. That caps adoption at teams
willing to refactor the thing they are already nervous about. An MCP client
speaks to servers over a socket, so a proxy can sit in that socket and give the
same guarantees to an agent that has no idea it is there.

THE BOUNDARY PROBLEM, stated plainly because it is the honest weak point:
MCP is request/response and has no notion of a transaction. Nothing in the
protocol says "this agent run failed, undo it". A single `tools/call` cannot
roll itself back -- by the time it returns, it is the thing that would need
undoing. So the boundary has to come from somewhere, and there are exactly
three places it can come from:

  * SESSION (default) -- the connection is the transaction. Effects accumulate
    while the agent works, and commit when it disconnects cleanly. A connection
    that drops with the saga open is a crashed agent, which is precisely the
    case the WAL and recovery daemon already exist for.
  * EXPLICIT -- the proxy injects `saga_commit` and `saga_rollback` into the
    tool list, so the model (or the application driving it) declares the
    boundary itself. Strictly better when the caller can be trusted to use it,
    and worthless when it cannot, which is why it is not the default.
  * NONE -- every call stands alone. Gate, limits and audit still apply;
    rollback does not. For read-heavy servers this is the honest setting.

An agent that never signals failure gets a durable, gated, audited log and no
rollback. That is a real limit of proxying, not something to paper over: the
proxy cannot infer that a model regretted something.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from ..context import SagaAborted, SagaContext
from ..gate import PreFlightGate, PreFlightViolation
from ..registry import compensator
from ..semantics import ActionSemantics, Compensation
from .policy import ProxyPolicy, ToolPolicy

logger = logging.getLogger("agent_saga.mcp")

Dispatcher = Callable[[str, str, dict], Awaitable[Any]]
"""(server, tool, arguments) -> result. How a compensation reaches an MCP
server -- from the proxy while it is running, or from the recovery daemon
afterwards."""

_DISPATCHER: Optional[Dispatcher] = None


def set_mcp_dispatcher(dispatcher: Optional[Dispatcher]) -> None:
    """Tell the compensation handler how to reach MCP servers.

    The recovery daemon runs in a different process from the proxy and holds no
    connections, so a compensation recovered from the WAL has nowhere to go
    until one is installed. Without it the daemon escalates rather than guesses
    -- the same stance as an unregistered connector handler.
    """
    global _DISPATCHER
    _DISPATCHER = dispatcher


@compensator("mcp.tool_call")
async def _compensate_via_mcp(*, server: Optional[str], tool: str,
                              arguments: dict, idempotency_key: str = "") -> Any:
    """Undo an MCP call by making another MCP call.

    Registered by name with JSON-serializable kwargs, so this survives the
    process boundary and the recovery daemon can run it after a crash -- which
    a closure over a live connection could not.
    """
    if _DISPATCHER is None:
        raise RuntimeError(
            f"no MCP dispatcher is installed, so {tool!r} cannot be compensated "
            f"from this process. Call agent_saga.mcp.set_mcp_dispatcher(...) in "
            f"the recovery daemon with a client that can reach the server.")
    return await _DISPATCHER(server, tool, arguments)


class ToolNotDeclared(PreFlightViolation):
    """An undeclared tool reached an enforcing proxy."""


class SagaMCPProxy:
    """Wraps a stream of MCP tool calls in a saga.

    Transport-free by design: it is handed a `call_upstream` coroutine and knows
    nothing about stdio, HTTP or the MCP SDK. That keeps the part that decides
    whether money moves testable without a subprocess, and lets the same core
    sit behind any transport.
    """

    CONTROL_TOOLS = ("saga_commit", "saga_rollback", "saga_status")

    def __init__(
        self,
        policy: ProxyPolicy,
        call_upstream: Callable[..., Awaitable[Any]],
        *,
        gate: Optional[PreFlightGate] = None,
        boundary: str = "session",
        wal: Any = None,
        server_name: str = "",
    ):
        if boundary not in ("session", "explicit", "none"):
            raise ValueError("boundary must be 'session', 'explicit' or 'none'")
        self.policy = policy
        self.call_upstream = call_upstream
        self.gate = gate or PreFlightGate()
        self.boundary = boundary
        self.server_name = server_name
        self._wal = wal
        self._ctx: Optional[SagaContext] = None
        self.observations: dict = {}
        """What OBSERVE saw, keyed by tool. The input to a policy skeleton."""
        self.blocked = 0
        self.forwarded = 0

    # -- lifecycle ---------------------------------------------------------

    async def _context(self) -> SagaContext:
        if self._ctx is None:
            self._ctx = SagaContext(gate=self.gate, wal=self._wal)
            await self._ctx.begin()
        return self._ctx

    async def commit(self) -> dict:
        """End the transaction, keeping every effect."""
        if self._ctx is None:
            return {"committed": 0, "note": "no saga was open"}
        steps = len(self._ctx.stack)
        await self._ctx.finish(aborted=False)
        self._ctx = None
        return {"committed": steps}

    async def rollback(self, reason: str = "client requested rollback") -> dict:
        """Unwind every compensable effect, LIFO."""
        if self._ctx is None:
            return {"rolled_back": 0, "clean": True, "note": "no saga was open"}
        ctx = self._ctx
        ctx.record_abort(RuntimeError(reason))
        report = await ctx.rollback()
        await ctx.finish(aborted=True, clean=report.clean)
        self._ctx = None
        return {
            "rolled_back": len(report.compensated),
            "clean": report.clean,
            "summary": report.summary(),
            "orphaned": [s.tool for s in report.orphaned],
            "failed": [s.tool for s in report.failed],
        }

    async def close(self, *, failed: bool = False) -> dict:
        """Called when the client goes away.

        A clean disconnect commits; a failed one rolls back. In `explicit` mode
        a still-open saga is rolled back rather than committed, because the
        caller opted into declaring boundaries and did not declare this one --
        and committing on a boundary nobody claimed is how a half-finished run
        becomes permanent.
        """
        if self._ctx is None:
            return {"note": "no saga was open"}
        if failed or self.boundary == "explicit":
            return await self.rollback(
                "client disconnected without committing" if not failed
                else "client signalled failure")
        return await self.commit()

    # -- tool list ---------------------------------------------------------

    def decorate_tools(self, upstream_tools: list) -> list:
        """Pass the upstream tool list through, adding control tools if the
        boundary is the model's to declare.

        Names and schemas are otherwise untouched: a model that sees a different
        tool list behaves differently, and the entire premise is that the agent
        cannot tell the proxy is there.
        """
        tools = list(upstream_tools)
        if self.boundary != "explicit":
            return tools
        tools.extend([
            {"name": "saga_commit",
             "description": "Commit the current transaction. Every effect so far "
                            "becomes permanent and can no longer be rolled back.",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "saga_rollback",
             "description": "Undo every reversible effect performed so far in "
                            "this transaction, most recent first.",
             "inputSchema": {"type": "object", "properties": {
                 "reason": {"type": "string"}}}},
            {"name": "saga_status",
             "description": "How many effects are pending in the current "
                            "transaction, and whether each can be undone.",
             "inputSchema": {"type": "object", "properties": {}}},
        ])
        return tools

    async def _control(self, tool: str, arguments: dict) -> Any:
        if tool == "saga_commit":
            return await self.commit()
        if tool == "saga_rollback":
            return await self.rollback(arguments.get("reason") or "model requested rollback")
        steps = [] if self._ctx is None else self._ctx.stack
        return {
            "open": self._ctx is not None,
            "steps": [{"tool": s.tool, "semantics": s.semantics.value,
                       "undoable": s.compensation is not None} for s in steps],
        }

    # -- the interception --------------------------------------------------

    async def call(self, tool: str, arguments: Optional[dict] = None) -> Any:
        """Intercept one `tools/call`.

        Raises `PreFlightViolation` for a refusal, which the transport turns
        into a protocol error the model can read -- refusal has to be legible to
        the caller, or it retries forever.
        """
        arguments = arguments or {}

        if self.boundary == "explicit" and tool in self.CONTROL_TOOLS:
            return await self._control(tool, arguments)

        policy = self.policy.get(tool)

        if self.policy.observing:
            self._observe(tool, arguments)
            self.forwarded += 1
            return await self.call_upstream(tool, arguments)

        if policy is None:
            if self.policy.unknown_semantics is None:
                self.blocked += 1
                logger.warning("refused undeclared tool %r", tool)
                raise ToolNotDeclared(_undeclared_decision(tool), _ctx_for(tool, arguments))
            policy = ToolPolicy(name=tool, semantics=self.policy.unknown_semantics)

        # A read needs gating and an audit record, but has no business on the
        # rollback stack: putting it there makes every rollback report list
        # searches as UNRESOLVED, which trains an operator to ignore the report.
        if self.boundary == "none" or (
                policy.semantics is ActionSemantics.REVERSIBLE
                and policy.compensate is None):
            return await self._call_gated_only(policy, tool, arguments)

        async def forward(**kwargs: Any) -> Any:
            # A real coroutine function, not a lambda returning one: the engine
            # routes non-coroutine callables to a worker thread, where a lambda
            # would hand back an un-awaited coroutine and the call would never
            # happen while the step recorded itself as committed.
            return await self.call_upstream(tool, kwargs)

        ctx = await self._context()
        try:
            return await ctx.execute(
                tool=tool,
                semantics=policy.semantics,
                forward=forward,
                forward_kwargs=arguments,
                compensate=self._factory(policy, arguments),
                policy_args=policy.gate_args(arguments),
            )
        except PreFlightViolation:
            self.blocked += 1
            raise

    async def _call_gated_only(self, policy: ToolPolicy, tool: str,
                               arguments: dict) -> Any:
        """Gate, limits and audit with no rollback -- the `none` boundary."""
        from ..gate import GateContext

        try:
            await self.gate.evaluate(GateContext(
                tool=tool, semantics=policy.semantics,
                kwargs=policy.gate_args(arguments)))
        except PreFlightViolation:
            self.blocked += 1
            raise
        self.forwarded += 1
        return await self.call_upstream(tool, arguments)

    def _factory(self, policy: ToolPolicy, arguments: dict):
        """Build the compensation factory from the policy's declaration.

        Returns None for a tool with no inverse, which the engine already
        reports as ORPHANED on rollback rather than silently skipping.
        """
        spec = policy.compensate
        if spec is None:
            return None

        def _make(result: Any) -> Optional[Compensation]:
            built = spec.build(result, arguments)
            missing = [k for k, v in built.items() if v is None]
            if missing:
                # The inverse cannot be addressed -- most often because the
                # forward result did not carry the id the policy points at.
                # Returning None makes the step ORPHANED and loud, which is the
                # honest outcome; sending a compensation with a null charge id
                # would fail at 3am instead, against a real API.
                logger.error(
                    "compensation for %r cannot be built: %s resolved to null "
                    "from the tool result. Check the paths in the policy.",
                    policy.name, ", ".join(missing))
                return None
            return Compensation(
                # In process, the proxy already holds the upstream connection,
                # so it compensates through it directly. `handler` is the same
                # operation by name, for the recovery daemon, which holds no
                # connections and resolves it through the registry instead.
                fn=self._compensate_now,
                handler="mcp.tool_call",
                kwargs={"server": spec.server or self.server_name,
                        "tool": spec.tool, "arguments": built},
            )

        return _make

    async def _compensate_now(self, *, server: Optional[str], tool: str,
                              arguments: dict, idempotency_key: str = "") -> Any:
        """Run a compensation over this proxy's own upstream connection."""
        return await self.call_upstream(tool, arguments)

    def _observe(self, tool: str, arguments: dict) -> None:
        seen = self.observations.setdefault(tool, {"calls": 0, "arg_keys": set()})
        seen["calls"] += 1
        seen["arg_keys"].update(arguments.keys())

    def policy_skeleton(self) -> dict:
        from .policy import skeleton_from_observations

        return skeleton_from_observations({
            k: {"calls": v["calls"], "arg_keys": sorted(v["arg_keys"])}
            for k, v in self.observations.items()})


def _ctx_for(tool: str, arguments: dict):
    from ..gate import GateContext

    return GateContext(tool=tool, semantics=ActionSemantics.IRREVERSIBLE,
                       kwargs=arguments)


def _undeclared_decision(tool: str):
    from ..gate import Decision, Verdict

    return Decision(
        Verdict.BLOCK, "tool-not-declared",
        f"{tool!r} is not declared in the proxy policy, so there is no "
        f"statement of whether it can be undone. Run the proxy in observe mode "
        f"to generate a skeleton, classify it, then enforce.")


__all__ = ["SagaMCPProxy", "ToolNotDeclared", "set_mcp_dispatcher", "Dispatcher",
           "SagaAborted"]
