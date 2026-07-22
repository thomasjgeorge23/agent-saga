"""Stripe connector -- the COMPENSABLE reference.

A charge is not reversible. A refund is a second, permanently visible ledger
entry, and the customer sees both. That is what COMPENSABLE means, and Stripe
is the clearest example of why the distinction is not pedantry: a bank's
reconciliation team must be able to explain the extra rows.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..reconcile import Observation, reconciler
from ..registry import compensator
from ..semantics import ActionSemantics, Compensation
from ._secrets import assert_no_secrets, resolve_credential

logger = logging.getLogger("agent_saga.connectors.stripe")


def _client(credential_ref: str):
    import stripe  # lazy: the daemon may not have every connector installed

    stripe.api_key = resolve_credential(credential_ref)
    return stripe


@compensator("stripe.refund")
def refund_charge(charge_id: str, amount: int, idempotency_key: str,
                  credential_ref: str) -> dict:
    """Refund a charge.

    Two independent idempotency guarantees, because one is not enough:

      1. `idempotency_key` is derived deterministically from the charge id, so
         the daemon retrying after a network blip sends a request Stripe
         recognizes as a duplicate and drops.
      2. `charge_already_refunded` is treated as success, not failure. Stripe
         retains idempotency keys for 24h; a daemon that comes back after a
         two-day outage gets a fresh key and would otherwise loop forever on
         an error that actually means "the job is already done".
    """
    stripe = _client(credential_ref)
    try:
        result = stripe.Refund.create(
            charge=charge_id,
            amount=amount,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        code = getattr(exc, "code", None) or getattr(exc, "error", None)
        code = getattr(code, "code", code)
        if code in ("charge_already_refunded", "charge_already_captured_refunded"):
            logger.info("charge %s was already refunded; treating as compensated",
                        charge_id)
            return {"charge_id": charge_id, "status": "already_refunded"}
        raise
    logger.info("refunded charge %s (%s)", charge_id, amount)
    return {"charge_id": charge_id, "refund_id": result["id"], "status": "refunded"}


def refund_key_for(charge_id: str) -> str:
    """Deterministic across processes and restarts: the daemon derives the same
    key the agent would have, without needing it recorded anywhere."""
    return f"agent-saga-refund-{charge_id}"


async def charge(
    ctx,
    *,
    customer_id: str,
    amount: int,
    credential_ref: str,
    currency: str = "usd",
    description: str = "AI agent automated charge",
    metadata: Optional[dict] = None,
) -> dict:
    """Charge a customer inside a saga, with a refund registered as its inverse.

    `amount` is in the currency's smallest unit (cents). Passing dollars here
    charges 100x -- a mistake worth gating on, which is exactly what a
    `arg_exceeds("amount", ...)` rule on the PreFlightGate is for.
    """
    stripe = _client(credential_ref)

    def _forward():
        return stripe.Charge.create(
            amount=amount,
            currency=currency,
            customer=customer_id,
            description=description,
            metadata={**(metadata or {}), "agent_saga_id": ctx.saga_id},
            # Forward idempotency is scoped to the saga+customer+amount so an
            # agent retrying its own tool call does not double-charge either.
            idempotency_key=f"agent-saga-charge-{ctx.saga_id}-{customer_id}-{amount}",
        )

    def _compensate(result: Any) -> Optional[Compensation]:
        if result is None:
            # UNKNOWN outcome: the charge may or may not exist. We cannot
            # refund by id because we never saw one. Stripe's own idempotency
            # record is the only thing that can resolve this -- escalate.
            logger.error(
                "charge for customer %s had an UNKNOWN outcome; no charge id was "
                "returned, so no refund can be issued automatically. Reconcile "
                "against idempotency key agent-saga-charge-%s-%s-%s",
                customer_id, ctx.saga_id, customer_id, amount)
            return None

        kwargs = {
            "charge_id": result["id"],
            "amount": amount,
            "idempotency_key": refund_key_for(result["id"]),
            "credential_ref": credential_ref,
        }
        assert_no_secrets(kwargs, where="stripe.charge")
        return Compensation(
            fn=refund_charge,
            handler="stripe.refund",
            kwargs=kwargs,
            description=f"refund charge {result['id']} ({amount} {currency})",
            idempotency_key=refund_key_for(result["id"]),
        )

    return await ctx.execute(
        tool="stripe.charge",
        semantics=ActionSemantics.COMPENSABLE,
        forward=_forward,
        compensate=_compensate,
        # Exposed to the gate so a rule like arg_exceeds("amount", 100_000) can
        # actually see the amount. Without this the closure hides it.
        policy_args={"amount": amount, "currency": currency,
                     "customer_id": customer_id},
    )


@reconciler("stripe.refund")
def observe_refund(*, charge_id: str, credential_ref: str, **_ignored) -> Observation:
    """Ask Stripe what actually happened to this charge.

    `refund_charge` returning success means Stripe acknowledged a request. It
    does not mean the money went back: an idempotency key can match a
    *different* request, and a refund can be created and then fail. The only
    authority on whether a charge is refunded is the charge.

    Accepts and ignores the other compensation kwargs (`amount`,
    `idempotency_key`) so the registry can pass a compensation's kwargs through
    unchanged -- a reconciler that had to be kept in signature-lockstep with its
    compensator would silently stop being run the first time one of them gained
    an argument.
    """
    stripe = _client(credential_ref)
    try:
        charge = stripe.Charge.retrieve(charge_id)
    except Exception as exc:
        message = str(exc)
        if "No such charge" in message or "resource_missing" in message:
            # Stripe has never heard of it. For a charge we believe we made,
            # that is drift; the caller decides, which is why this reports the
            # fact rather than an interpretation.
            return Observation(exists=False, reversed_=False,
                               detail="Stripe has no such charge")
        raise

    refunded = bool(_get(charge, "refunded"))
    amount = _get(charge, "amount")
    amount_refunded = _get(charge, "amount_refunded") or 0
    # A partial refund is not a reversal. Saying so plainly avoids a report
    # that calls a half-returned payment "confirmed".
    if not refunded and amount_refunded:
        return Observation(
            exists=True, reversed_=False, amount=amount,
            detail=f"PARTIALLY refunded: {amount_refunded} of {amount}")
    return Observation(
        exists=True, reversed_=refunded, amount=amount,
        detail=f"status={_get(charge, 'status')}, refunded={refunded}")


def _get(obj: Any, key: str) -> Any:
    """Stripe objects are dict-like, but a test double may be a plain object."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


__all__ = ["charge", "refund_charge", "refund_key_for", "observe_refund"]
