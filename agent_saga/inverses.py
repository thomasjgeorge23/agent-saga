"""Declarative inverses: say what undoes a tool once, not at every call site.

The most-cited friction in this library is real. Declaring semantics is
load-bearing and stays; hand-writing a compensation *factory* for every tool
is ceremony, and most of it is the same shape over and over:

    # before -- eight lines, at every call site
    charge = kit.safe_tool(
        stripe_charge, semantics="COMPENSABLE",
        compensate=lambda r: Compensation(
            fn=refund_charge,
            handler="stripe.refund",
            kwargs={"charge_id": r["id"]},
            description=f"refund {r['id']}"))

    # after -- declared once, next to the function it undoes
    @inverse_of(stripe_charge, maps={"charge_id": "id"})
    @compensator("stripe.refund")
    def refund_charge(charge_id: str): ...

    charge = kit.safe_tool(stripe_charge, semantics="COMPENSABLE")

What this does **not** do, deliberately:

  * **It never guesses semantics.** Whether an effect is REVERSIBLE,
    COMPENSABLE, or IRREVERSIBLE is the one judgement the engine refuses to
    make for you, and inferring it from a function name is exactly the kind of
    plausible default that gets a control trusted by nobody who chose it.
  * **It never produces an unrecoverable compensation silently.** An inverse
    must be registered with `@compensator`, because a closure cannot survive
    `kill -9` and a recovery daemon has only the WAL. Pairing an unregistered
    function raises here, at import time, rather than at 3am.
  * **It never invents a mapping.** If the forward result does not contain the
    field an inverse needs, the compensation factory raises with both the field
    it wanted and the keys it actually saw -- the alternative is a rollback
    that runs with `None` and reports success.

Nothing here is required: `compensate=` still works and still wins when both
are present.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Mapping, Optional

from .semantics import Compensation

logger = logging.getLogger("agent_saga.inverses")

__all__ = [
    "InverseError",
    "auto_compensation",
    "call_with",
    "delete_by",
    "has_inverse",
    "inverse_of",
    "map_result",
]


class InverseError(RuntimeError):
    """An inverse could not be paired or applied. Raised at declaration time
    where possible, and loudly at rollback time otherwise -- never swallowed
    into a compensation that runs with missing arguments."""


#: forward callable -> factory. Keyed by the function object, so two tools that
#: share a name in different modules cannot collide.
_INVERSES: "Dict[int, Callable[[Any], Optional[Compensation]]]" = {}
_FORWARD_NAMES: Dict[int, str] = {}


def _handler_name(fn: Callable[..., Any]) -> str:
    name = getattr(fn, "__compensator_name__", None)
    if not name:
        raise InverseError(
            f"{getattr(fn, '__qualname__', fn)!r} is not a registered "
            f"compensation handler, so a compensation built from it could not "
            f"be run by saga-recoveryd after a crash -- the closure dies with "
            f"the process. Decorate it with @compensator(\"a.stable.name\") "
            f"first; the name must stay stable across deploys or in-flight "
            f"sagas become unrecoverable.")
    return name


def map_result(inverse: Callable[..., Any],
               maps: Optional[Mapping[str, str]] = None,
               *,
               static: Optional[Mapping[str, Any]] = None,
               description: Optional[str] = None
               ) -> Callable[[Any], Optional[Compensation]]:
    """Build a compensation factory that pulls the inverse's arguments out of
    the forward result.

    `maps` is ``{inverse_kwarg: result_key}``; `static` adds fixed arguments.
    A missing key raises rather than passing ``None`` into a refund.

    On an UNKNOWN forward outcome the result is ``None`` -- the forward call
    raised or timed out and may still have landed. A mapping-based inverse
    cannot know the id in that case, so the factory returns None and the step
    is reported ORPHANED rather than compensated with invented arguments. When
    the inverse needs no result (a fixed target), use `call_with`, which stays
    valid in exactly that situation.
    """
    handler = _handler_name(inverse)
    mapping = dict(maps or {})
    fixed = dict(static or {})

    def factory(result: Any) -> Optional[Compensation]:
        if mapping and result is None:
            logger.warning(
                "no forward result for %s, so its inverse cannot be derived; "
                "the step will be reported ORPHANED rather than compensated "
                "with guessed arguments", handler)
            return None

        kwargs: Dict[str, Any] = dict(fixed)
        for parameter, key in mapping.items():
            try:
                kwargs[parameter] = result[key]
            except (KeyError, IndexError, TypeError) as exc:
                available = (sorted(result.keys())
                             if isinstance(result, Mapping) else type(result).__name__)
                raise InverseError(
                    f"inverse {handler!r} needs {parameter!r} from the forward "
                    f"result key {key!r}, which is not there. The result "
                    f"provided: {available}. Refusing to compensate with a "
                    f"missing argument.") from exc

        return Compensation(
            fn=inverse, handler=handler, kwargs=kwargs,
            description=description or f"{handler}({', '.join(sorted(kwargs))})")

    factory.__qualname__ = f"inverse[{handler}]"
    return factory


def delete_by(inverse: Callable[..., Any], *, id_field: str = "id",
              param: Optional[str] = None,
              description: Optional[str] = None
              ) -> Callable[[Any], Optional[Compensation]]:
    """The commonest shape: the forward call created something and returned its
    id; the inverse deletes it.

        compensate=delete_by(delete_instance, id_field="InstanceId")
    """
    return map_result(inverse, {param or id_field: id_field},
                      description=description)


def call_with(inverse: Callable[..., Any], **kwargs: Any
              ) -> Callable[[Any], Optional[Compensation]]:
    """An inverse whose arguments are known before the forward call runs.

    Valid on an UNKNOWN outcome too, which is the point: the target was already
    determined, so the compensation is correct whether the forward call landed,
    half-landed, or raised. Make it idempotent.
    """
    handler = _handler_name(inverse)

    def factory(_result: Any) -> Optional[Compensation]:
        return Compensation(
            fn=inverse, handler=handler, kwargs=dict(kwargs),
            description=f"{handler}({', '.join(sorted(kwargs))})")

    factory.__qualname__ = f"inverse[{handler}]"
    return factory


def inverse_of(forward: Callable[..., Any],
               maps: Optional[Mapping[str, str]] = None,
               *,
               static: Optional[Mapping[str, Any]] = None,
               description: Optional[str] = None) -> Callable:
    """Decorator: declare that this function undoes `forward`.

        @inverse_of(create_user, maps={"user_id": "id"})
        @compensator("users.delete")
        def delete_user(user_id: str): ...

    After this, `kit.safe_tool(create_user, semantics="COMPENSABLE")` needs no
    `compensate=` -- the pairing is found automatically. An explicit
    `compensate=` still wins, so a call site can always override.

    Order matters: `@compensator` must be applied first (i.e. listed below), so
    the handler name exists when the pairing is made.
    """

    def decorate(inverse: Callable[..., Any]) -> Callable[..., Any]:
        factory = map_result(inverse, maps, static=static, description=description)
        key = id(forward)
        existing = _INVERSES.get(key)
        if existing is not None and existing is not factory:
            raise InverseError(
                f"{getattr(forward, '__qualname__', forward)!r} already has an "
                f"inverse ({existing.__qualname__}). Two inverses for one "
                f"forward action is ambiguous; pass compensate= at the call "
                f"site for the exceptional one.")
        _INVERSES[key] = factory
        _FORWARD_NAMES[key] = getattr(forward, "__qualname__", repr(forward))
        setattr(inverse, "__inverse_of__", forward)
        logger.debug("paired inverse %s -> %s", _FORWARD_NAMES[key], factory.__qualname__)
        return inverse

    return decorate


def has_inverse(forward: Callable[..., Any]) -> bool:
    """Whether a declarative inverse is registered for this callable."""
    return id(forward) in _INVERSES


def auto_compensation(forward: Callable[..., Any]
                      ) -> Optional[Callable[[Any], Optional[Compensation]]]:
    """The compensation factory paired with `forward`, or None.

    `AgentKit.safe_tool` consults this when no `compensate=` is given. Finding
    one can only improve the outcome: without it the step would have been
    reported ORPHANED on rollback, so this never weakens a guarantee, it
    supplies one that was otherwise absent.
    """
    return _INVERSES.get(id(forward))
