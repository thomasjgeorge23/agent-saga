"""Salesforce connector -- REST state-diff restoration.

Three things in the reference spec would fail against a real org:

  1. A Salesforce GET returns read-only fields (`attributes`, `Id`,
     `CreatedDate`, `LastModifiedDate`, `SystemModstamp`, formula fields).
     PATCHing that payload back returns 400 INVALID_FIELD_FOR_INSERT_UPDATE.
     The revert must be filtered to writable fields.
  2. Restoring the *whole* object reverts fields this saga never touched. If a
     human edited the Description while the agent was working, a full restore
     silently discards their edit. Restore only the keys we patched.
  3. The instance host is per-org and per-environment. It cannot be a constant.

Also: this is COMPENSABLE, not REVERSIBLE. Salesforce fires workflow rules,
flows, and outbound messages on the forward PATCH. Those already happened and
restoring the field values does not recall them.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..registry import compensator
from ..semantics import ActionSemantics, Compensation
from ._secrets import assert_no_secrets, resolve_credential

logger = logging.getLogger("agent_saga.connectors.salesforce")

DEFAULT_API_VERSION = "v60.0"

# Never writable on a PATCH. Sending any of these is an immediate 400.
READ_ONLY_FIELDS = frozenset({
    "attributes", "Id", "CreatedDate", "CreatedById", "LastModifiedDate",
    "LastModifiedById", "SystemModstamp", "LastViewedDate", "LastReferencedDate",
    "IsDeleted", "OwnerId",
})


class StaleObject(RuntimeError):
    """The record changed after we wrote it. Reverting would discard whoever
    edited it in the meantime."""


def _url(instance_url: str, object_type: str, object_id: str, api_version: str) -> str:
    return (f"{instance_url.rstrip('/')}/services/data/{api_version}"
            f"/sobjects/{object_type}/{object_id}")


def writable(payload: dict) -> dict:
    """Strip fields Salesforce refuses on write."""
    return {k: v for k, v in payload.items() if k not in READ_ONLY_FIELDS}


@compensator("salesforce.revert_object")
async def revert_object(
    instance_url: str,
    object_type: str,
    object_id: str,
    previous_values: dict,
    expected_modstamp: Optional[str],
    credential_ref: str,
    api_version: str = DEFAULT_API_VERSION,
) -> dict:
    """Restore the fields this saga changed, if nobody has changed them since.

    `expected_modstamp` is the record's LastModifiedDate as of our own write.
    If it has moved, someone edited the record after us and a blind revert
    would destroy their edit -- so we refuse and escalate. Only a human knows
    whose version should win.

    Async-native: the engine awaits a coroutine compensation directly instead of
    parking it on a worker thread. At fleet scale that is the difference between
    a rollback storm being bounded by the thread pool and being bounded by the
    remote API.
    """
    import httpx  # lazy: the daemon may not ship every connector's deps

    token = resolve_credential(credential_ref)
    headers = {"Authorization": f"Bearer {token}"}
    url = _url(instance_url, object_type, object_id, api_version)
    body = writable(previous_values)
    if not body:
        return {"object_id": object_id, "status": "nothing_writable_to_restore"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        if expected_modstamp:
            current = await client.get(url, headers=headers,
                                       params={"fields": "LastModifiedDate"})
            current.raise_for_status()
            actual = current.json().get("LastModifiedDate")
            if actual and actual != expected_modstamp:
                raise StaleObject(
                    f"{object_type} {object_id} was modified after this saga wrote "
                    f"it (LastModifiedDate {actual} != {expected_modstamp}). "
                    f"Refusing to revert and discard that change."
                )

        resp = await client.patch(url, json=body, headers=headers)
        resp.raise_for_status()

    logger.info("reverted %s %s (%d field(s))", object_type, object_id, len(body))
    return {"object_id": object_id, "status": "reverted", "fields": sorted(body)}


async def patch_object(
    ctx,
    *,
    instance_url: str,
    object_type: str,
    object_id: str,
    patch: dict,
    credential_ref: str,
    api_version: str = DEFAULT_API_VERSION,
    client=None,
) -> dict:
    """Patch a Salesforce object inside a saga, capturing the prior values of
    exactly the fields being changed."""
    if not patch:
        raise ValueError("patch must not be empty")

    import httpx

    token = resolve_credential(credential_ref)
    headers = {"Authorization": f"Bearer {token}"}
    url = _url(instance_url, object_type, object_id, api_version)
    # Fetch only what we are about to overwrite, plus the concurrency stamp.
    fields = sorted(set(patch) | {"LastModifiedDate"})

    async def _forward():
        owned = client is None
        http = client or httpx.AsyncClient(timeout=30.0)
        try:
            before = await http.get(url, headers=headers,
                                    params={"fields": ",".join(fields)})
            before.raise_for_status()
            snapshot = before.json()

            resp = await http.patch(url, json=patch, headers=headers)
            resp.raise_for_status()

            # Re-read the stamp so the guard reflects OUR write, not the state
            # before it. Comparing against the pre-write stamp would flag our
            # own change as a concurrent edit and block every rollback.
            after = await http.get(url, headers=headers,
                                   params={"fields": "LastModifiedDate"})
            after.raise_for_status()

            return {
                "previous_values": {k: snapshot.get(k) for k in patch},
                "modstamp_after_our_write": after.json().get("LastModifiedDate"),
            }
        finally:
            if owned:
                await http.aclose()

    def _compensate(result: Any) -> Optional[Compensation]:
        if result is None:
            logger.error(
                "patch to %s %s had an UNKNOWN outcome; no snapshot was captured "
                "so the record must be reconciled by hand", object_type, object_id)
            return None

        kwargs = {
            "instance_url": instance_url,
            "object_type": object_type,
            "object_id": object_id,
            "previous_values": writable(result["previous_values"]),
            "expected_modstamp": result["modstamp_after_our_write"],
            "credential_ref": credential_ref,
            "api_version": api_version,
        }
        assert_no_secrets(kwargs, where="salesforce.patch_object")
        return Compensation(
            fn=revert_object,
            handler="salesforce.revert_object",
            kwargs=kwargs,
            description=f"revert {object_type} {object_id} ({sorted(patch)})",
        )

    return await ctx.execute(
        tool="salesforce.patch_object",
        # Workflow rules and outbound messages already fired on the forward
        # PATCH. Restoring field values does not recall them.
        semantics=ActionSemantics.COMPENSABLE,
        forward=_forward,
        compensate=_compensate,
        policy_args={"object_type": object_type, "object_id": object_id,
                     "fields": sorted(patch)},
    )


__all__ = ["patch_object", "revert_object", "writable", "StaleObject",
           "READ_ONLY_FIELDS"]
