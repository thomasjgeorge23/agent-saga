"""PostgreSQL connector -- snapshot-and-restore without holding a transaction.

The spec called this the REVERSIBLE reference. It is not, and the distinction
is load-bearing:

  * Other sessions read the mutated row during the agent's thinking time.
  * ON UPDATE triggers fire. `updated_at` moves. Audit tables gain rows.
  * Logical replication and CDC ship the intermediate state downstream, where
    it may already have triggered a webhook you cannot recall.

REVERSIBLE must mean "no observer can tell it happened" -- an in-process cache,
a scratch table nobody else reads. A live row in a shared database is
COMPENSABLE: we can restore the value, but the event was witnessed.

That is not pedantry about vocabulary. REVERSIBLE steps deliberately skip the
fsync barrier (see wal.py), so classifying a Postgres write as REVERSIBLE would
make it silently unrecoverable after a crash -- the exact orphan saga-recoveryd
exists to catch.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional, Sequence

from ..registry import compensator
from ..semantics import ActionSemantics, Compensation
from ._secrets import assert_no_secrets, resolve_credential

logger = logging.getLogger("agent_saga.connectors.postgres")

# An LLM chooses these at runtime. Identifiers cannot be parameterized, so they
# are validated against an allowlist pattern and quoted -- never interpolated
# raw. This is a live injection surface in a way it is not in ordinary apps,
# because in ordinary apps a developer wrote the table name.
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class ConcurrentModification(RuntimeError):
    """The row changed after we wrote it. Restoring now would silently discard
    somebody else's write -- a lost update caused by the rollback itself."""


def _ident(name: str, *, kind: str) -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise ValueError(
            f"unsafe {kind} identifier {name!r}. Identifiers cannot be bound as "
            f"parameters, so agent-supplied names are restricted to "
            f"[A-Za-z_][A-Za-z0-9_$]*"
        )
    return f'"{name}"'


def _qualified(table: str) -> str:
    """Accepts `table` or `schema.table`, quoting each part separately."""
    parts = table.split(".")
    if len(parts) > 2:
        raise ValueError(f"unsafe table identifier {table!r}")
    return ".".join(_ident(p, kind="table") for p in parts)


def _connect(credential_ref: str):
    dsn = resolve_credential(credential_ref)
    try:
        import psycopg  # psycopg 3

        return psycopg.connect(dsn), "psycopg"
    except ImportError:
        import psycopg2

        return psycopg2.connect(dsn), "psycopg2"


@compensator("postgres.restore_row")
def restore_row(
    table: str,
    pk_column: str,
    pk_value: Any,
    previous_state: dict,
    expected_current: dict,
    credential_ref: str,
) -> dict:
    """Restore the columns we changed, and only if nobody else changed them.

    The guard is the whole point. A blind `UPDATE ... WHERE id = X` would
    overwrite any write that landed between the agent's mutation and this
    rollback -- turning a rollback into data loss. Instead we require the row to
    still hold exactly what we wrote; if it does not, we refuse and escalate to
    a human, because only a human knows whose write should win.
    """
    tbl = _qualified(table)
    pk = _ident(pk_column, kind="column")
    cols = list(previous_state)

    set_sql = ", ".join(f"{_ident(c, kind='column')} = %s" for c in cols)
    guard_sql = " AND ".join(
        f"{_ident(c, kind='column')} IS NOT DISTINCT FROM %s" for c in expected_current
    )
    sql = f"UPDATE {tbl} SET {set_sql} WHERE {pk} = %s AND {guard_sql}"
    params = [previous_state[c] for c in cols] + [pk_value] + \
             [expected_current[c] for c in expected_current]

    conn, driver = _connect(credential_ref)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            affected = cur.rowcount
        conn.commit()
    finally:
        conn.close()

    if affected == 0:
        raise ConcurrentModification(
            f"{table}.{pk_column}={pk_value!r} no longer holds the values this "
            f"saga wrote; another writer has modified it. Refusing to restore "
            f"and discard their change. Expected {expected_current!r}."
        )

    logger.info("restored %s row %s=%r (%d column(s), driver=%s)",
                table, pk_column, pk_value, len(cols), driver)
    return {"table": table, "pk_value": pk_value, "restored_columns": cols}


async def update_row(
    ctx,
    *,
    pool,
    table: str,
    pk_column: str,
    pk_value: Any,
    updates: dict,
    credential_ref: str,
) -> dict:
    """Update a row inside a saga, capturing its prior state as the inverse.

    Snapshot and mutation happen in one short autocommit round trip. No
    transaction is held open across the agent's thinking time -- that would pin
    a connection and hold row locks for however long the model takes, which is
    how you exhaust a pool and stall unrelated traffic.

    `pool` is an asyncpg-style pool for the forward path; the compensation runs
    through a fresh sync connection because the daemon has no access to the
    agent's pool.
    """
    if not updates:
        raise ValueError("updates must not be empty")

    tbl = _qualified(table)
    pk = _ident(pk_column, kind="column")
    cols = list(updates)
    # Snapshot only the columns we are about to touch. `SELECT *` would drag in
    # generated and identity columns, which are not writable on restore.
    select_cols = ", ".join(_ident(c, kind="column") for c in cols)

    async def _forward():
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {select_cols} FROM {tbl} WHERE {pk} = $1", pk_value
            )
            if row is None:
                raise LookupError(f"no row in {table} where {pk_column} = {pk_value!r}")

            set_sql = ", ".join(
                f"{_ident(c, kind='column')} = ${i + 2}" for i, c in enumerate(cols)
            )
            await conn.execute(
                f"UPDATE {tbl} SET {set_sql} WHERE {pk} = $1",
                pk_value, *[updates[c] for c in cols],
            )
            return dict(row)

    def _compensate(previous_state: Any) -> Optional[Compensation]:
        if previous_state is None:
            # UNKNOWN: we do not know whether the UPDATE landed, and we never
            # got the prior values. There is nothing safe to write back.
            logger.error(
                "update to %s (%s=%r) had an UNKNOWN outcome and no snapshot was "
                "captured; the row must be reconciled by hand",
                table, pk_column, pk_value)
            return None

        kwargs = {
            "table": table,
            "pk_column": pk_column,
            "pk_value": pk_value,
            "previous_state": previous_state,
            # What we wrote -- the guard that makes restore refuse to clobber
            # a concurrent writer.
            "expected_current": dict(updates),
            "credential_ref": credential_ref,
        }
        assert_no_secrets(kwargs, where="postgres.update_row")
        return Compensation(
            fn=restore_row,
            handler="postgres.restore_row",
            kwargs=kwargs,
            description=f"restore {table}.{pk_column}={pk_value!r} ({len(cols)} cols)",
        )

    return await ctx.execute(
        tool="postgres.update_row",
        # COMPENSABLE, not REVERSIBLE. See the module docstring.
        semantics=ActionSemantics.COMPENSABLE,
        forward=_forward,
        compensate=_compensate,
    )


__all__ = ["update_row", "restore_row", "ConcurrentModification"]
