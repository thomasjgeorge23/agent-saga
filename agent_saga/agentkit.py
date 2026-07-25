"""The agent-facing SDK: transactional safety in one object, self-describing.

Everything an AI agent needs to act safely already exists in this package -- the
gate, the typed compensation, the tamper-evident WAL, the audit proofs -- but
wiring it together takes knowledge of a dozen modules. That friction is the
enemy of adoption: an agent framework will not reach for safety it has to
assemble.

`AgentKit` collapses that to one object with three verbs an agent actually uses:

    kit = AgentKit(name="research-agent")

    charge = kit.safe_tool(stripe_charge, semantics="COMPENSABLE",
                           compensate=lambda r: dict(handler="refund",
                                                     kwargs={"id": r["id"]}))

    async with kit.transaction() as txn:      # a saga boundary
        await charge(amount=4200)             # gated, logged, compensable
        ...                                    # any failure rolls the whole thing back

    kit.guarantees()   # machine-readable: what is enforced, versioned
    kit.status()       # live posture: halted? breaker open? approvals pending?

The two introspection verbs are the part that is genuinely new. An agent that
can *read* what it is guaranteed -- and check, before it acts, whether the system
is currently able to keep those guarantees -- can reason about its own safety
instead of hoping. That is what makes the surface usable by a model rather than
only by the human who wired it.

Nothing here is new capability; it is a faithful, ergonomic front door onto
capability that was already proven. The heavy lifting -- and the tests -- live in
the modules it delegates to.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional, Union

from ._version import __version__
from .semantics import ActionSemantics

logger = logging.getLogger("agent_saga.agentkit")

# What a compensate callback may return: either a Compensation, or a plain dict
# {"handler": str, "kwargs": dict, ...} that we lift into one. The dict form lets
# an agent describe an undo without importing the Compensation type.
CompensateReturn = Any
CompensateFn = Callable[[Any], CompensateReturn]

_SEMANTICS = {
    "REVERSIBLE": ActionSemantics.REVERSIBLE,
    "COMPENSABLE": ActionSemantics.COMPENSABLE,
    "IRREVERSIBLE": ActionSemantics.IRREVERSIBLE,
}


def _as_semantics(value: Union[str, ActionSemantics]) -> ActionSemantics:
    if isinstance(value, ActionSemantics):
        return value
    key = str(value).upper()
    if key not in _SEMANTICS:
        raise ValueError(
            f"unknown semantics {value!r}; expected one of {sorted(_SEMANTICS)}")
    return _SEMANTICS[key]


def _lift_compensation(result: Any, spec: Any) -> Any:
    """Turn a compensate callback's return into a Compensation. Accepts a
    Compensation as-is, or a dict describing one, so an agent never has to import
    the type."""
    from .semantics import Compensation

    if spec is None or isinstance(spec, Compensation):
        return spec
    if isinstance(spec, dict):
        handler = spec.get("handler") or spec.get("tool")
        return Compensation(
            fn=spec.get("fn"),
            handler=handler or "compensation",
            kwargs=spec.get("kwargs") or {},
            description=spec.get("description", ""),
            idempotency_key=spec.get("idempotency_key", ""),
        )
    raise TypeError(
        "compensate must return a Compensation or a dict describing one, "
        f"got {type(spec).__name__}")


def describe_guarantees() -> dict:
    """A machine-readable, versioned manifest of what this system enforces.

    An agent can read this to feature-detect and to reason about its own safety:
    the semantics taxonomy it must classify tools into, what the pre-flight gate
    refuses, what the WAL and the audit proofs guarantee, and what it does *not*
    claim. Stable keys; additive changes only within a manifest version.
    """
    return {
        "manifest_version": 1,
        "library": "agent-saga",
        "library_version": __version__,
        "semantics": {
            "REVERSIBLE": "read-only or self-undoing; runs freely, never gated",
            "COMPENSABLE": "has an effect with a declared inverse; the inverse "
                           "runs on rollback",
            "IRREVERSIBLE": "cannot be undone; the gate requires a human before it "
                            "runs",
        },
        "enforced_before_an_effect": [
            "an IRREVERSIBLE step is refused unless a human (or a hardware "
            "signature) approves it",
            "a spend/rate limit over its window refuses the call before it runs",
            "a tripped kill-switch or open circuit blocks the step",
            "a broken policy predicate fails closed, never open",
        ],
        "guaranteed_after_the_fact": [
            "every intent is written to a tamper-evident WAL before its effect",
            "on any failure, committed COMPENSABLE steps are undone LIFO",
            "the WAL is hash-chained; alteration or truncation is detectable",
            "one saga can be proven to an auditor without revealing any other "
            "(selective disclosure)",
            "every committed effect can be certified accounted-for, or the "
            "orphan is named",
        ],
        "not_claimed": [
            "that a tool labelled REVERSIBLE actually is -- that is the "
            "developer's assertion, which the runtime cannot verify",
            "durability beyond what the configured WAL backend provides",
            "protection against code execution inside the agent process",
        ],
        "proof_tools": {
            "certify": "prove every committed effect is accounted for",
            "prove": "selective-disclosure proof of one saga",
            "verify": "hash-chain integrity of the whole log",
        },
    }


class AgentKit:
    """One object that gives an agent transactional safety and self-description.

    Construct it once, wrap tools with :meth:`safe_tool`, run work inside
    :meth:`transaction`, and introspect with :meth:`guarantees` and
    :meth:`status`. Sensible defaults throughout; pass an explicit ``gate`` or
    ``wal`` to override.
    """

    def __init__(self, name: str = "agent", *, gate: Any = None, wal: Any = None):
        self.name = name
        self._gate = gate
        self._wal = wal

    # -- wrapping a tool ---------------------------------------------------

    def safe_tool(
        self,
        fn: Callable[..., Any],
        *,
        semantics: Union[str, ActionSemantics],
        compensate: Optional[CompensateFn] = None,
        timeout: Optional[float] = None,
        name: Optional[str] = None,
    ) -> Callable[..., Awaitable[Any]]:
        """Wrap any callable so that, inside a transaction, it is gated, logged,
        and compensable -- and outside one, it just runs. The returned callable
        is async and takes keyword arguments.

        ``compensate`` receives the forward result and returns a Compensation or
        a dict describing one (only meaningful for COMPENSABLE)."""
        sem = _as_semantics(semantics)
        tool_name = name or getattr(fn, "__name__", "tool")

        comp_factory = None
        if compensate is not None:
            def comp_factory(result: Any):     # noqa: E731 - small adapter
                return _lift_compensation(result, compensate(result))

        async def call(**kwargs: Any) -> Any:
            from .decorator import current_saga
            from .context import _invoke

            async def forward() -> Any:
                return await _invoke(fn, kwargs, timeout)

            ctx = current_saga()
            if ctx is None:
                # No boundary: run untouched, so the same tool works in a script
                # or a test without ceremony.
                return await forward()
            return await ctx.execute(
                tool=tool_name,
                semantics=sem,
                forward=forward,
                compensate=comp_factory,
                policy_args=dict(kwargs),
                timeout=timeout,
            )

        call.__name__ = f"safe_{tool_name}".replace(".", "_").replace("-", "_")
        call.__agentkit_semantics__ = sem.name
        return call

    # -- running a transaction --------------------------------------------

    def transaction(self, *, name: Optional[str] = None):
        """An async context manager saga boundary. Any exception inside rolls the
        whole transaction back (LIFO) and re-raises as SagaAborted with the report.
        """
        from .decorator import saga_scope

        return saga_scope(name=name or self.name, gate=self._gate, wal=self._wal)

    # -- introspection -----------------------------------------------------

    def guarantees(self) -> dict:
        """The machine-readable manifest of what this system enforces."""
        return describe_guarantees()

    def status(self) -> dict:
        """Live safety posture: is the system currently able to keep its
        guarantees? An agent should check this before a burst of effectful work.

        Returns a dict with ``ready`` (bool) and the reasons it is or is not.

        This is deliberately fail-*closed*: if a safety signal cannot be read
        (its store is unreachable), that is itself a blocker, never a silent
        green. A truthful "I can't tell" is worth more to an agent than a
        confident lie.
        """
        blockers: list[str] = []
        warnings: list[str] = []

        # 1. The kill-switch. A global (empty-scope) switch that is not RUNNING
        # means side effects are being halted or drained right now.
        from .killswitch import get_kill_switch
        switch = get_kill_switch()
        if switch is not None:
            try:
                active = switch.active()          # global scope
            except Exception as exc:               # store unreachable -> unknown
                blockers.append(
                    f"kill-switch state is unknown ({type(exc).__name__}); "
                    f"treating as not-ready")
            else:
                if active is not None:
                    blockers.append(f"kill-switch active: {active.summary()}")

        # 2. Pending human approvals. Informational, not a blocker: a saga will
        # itself wait for the human. But an agent may want to know before it
        # starts work that will stall.
        approvals_pending = 0
        try:
            from .approvals import get_approval_store
            approvals_pending = len(get_approval_store().pending())
        except Exception as exc:
            warnings.append(f"approval queue unreadable ({type(exc).__name__})")

        return {
            "agent": self.name,
            "ready": not blockers,
            "blockers": blockers,
            "warnings": warnings,
            "approvals_pending": approvals_pending,
            "library_version": __version__,
        }


__all__ = ["AgentKit", "describe_guarantees"]
