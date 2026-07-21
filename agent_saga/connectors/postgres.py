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
        # A gate may restrict which tables an agent can mutate, or how many
        # columns at once. The closure would otherwise hide all of it.
        policy_args={"table": table, "pk_column": pk_column,
                     "pk_value": pk_value, "columns": list(updates)},
    )


# ==========================================================================
# INSERT -> DELETE
# ==========================================================================

@compensator("postgres.delete_inserted_row")
def delete_inserted_row(table: str, pk: dict, credential_ref: str) -> dict:
    """Undo an insert by deleting the row we created. Idempotent: zero rows
    affected means it is already gone, which is success, not failure."""
    tbl = _qualified(table)
    where = " AND ".join(f"{_ident(c, kind='column')} = %s" for c in pk)
    conn, driver = _connect(credential_ref)
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {tbl} WHERE {where}", list(pk.values()))
            affected = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    logger.info("deleted inserted %s row pk=%r (%d row(s), driver=%s)",
                table, pk, affected, driver)
    return {"table": table, "pk": pk, "deleted": affected}


async def insert_row(
    ctx,
    *,
    pool,
    table: str,
    values: dict,
    pk_columns: list[str],
    credential_ref: str,
) -> dict:
    """Insert a row inside a saga, registering a DELETE of exactly that row as
    the inverse. `pk_columns` names the primary key (compound keys allowed); the
    inserted PK is read back via RETURNING so a serial/identity key is known."""
    if not values:
        raise ValueError("values must not be empty")

    tbl = _qualified(table)
    cols = list(values)
    col_sql = ", ".join(_ident(c, kind="column") for c in cols)
    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
    returning = ", ".join(_ident(c, kind="column") for c in pk_columns)

    async def _forward():
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"INSERT INTO {tbl} ({col_sql}) VALUES ({placeholders}) RETURNING {returning}",
                *[values[c] for c in cols],
            )
            return dict(row)

    def _compensate(pk_row: Any) -> Optional[Compensation]:
        if pk_row is None:
            # UNKNOWN. If the PK was caller-supplied we can still delete by it;
            # a serial key we never read back cannot be targeted -> escalate.
            if all(c in values for c in pk_columns):
                pk = {c: values[c] for c in pk_columns}
            else:
                logger.error(
                    "insert into %s had an UNKNOWN outcome and the PK is "
                    "server-generated; cannot target a delete. Reconcile by hand.",
                    table)
                return None
        else:
            pk = dict(pk_row)

        kwargs = {"table": table, "pk": pk, "credential_ref": credential_ref}
        assert_no_secrets(kwargs, where="postgres.insert_row")
        return Compensation(
            fn=delete_inserted_row,
            handler="postgres.delete_inserted_row",
            kwargs=kwargs,
            description=f"delete inserted {table} row pk={pk!r}",
        )

    return await ctx.execute(
        tool="postgres.insert_row",
        semantics=ActionSemantics.COMPENSABLE,
        forward=_forward,
        compensate=_compensate,
        policy_args={"table": table, "columns": cols},
    )


# ==========================================================================
# DELETE -> REINSERT
# ==========================================================================

@compensator("postgres.reinsert_row")
def reinsert_row(table: str, row: dict, pk_columns: list, credential_ref: str) -> dict:
    """Undo a delete by re-inserting the captured row -- unless a row with the
    same key already exists, which means the delete never landed (UNKNOWN) or
    someone recreated it. Reinserting then would either duplicate or overwrite,
    so we refuse and escalate."""
    tbl = _qualified(table)
    where = " AND ".join(f"{_ident(c, kind='column')} = %s" for c in pk_columns)
    cols = list(row)
    col_sql = ", ".join(_ident(c, kind="column") for c in cols)
    placeholders = ", ".join("%s" for _ in cols)

    conn, driver = _connect(credential_ref)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {tbl} WHERE {where}",
                        [row[c] for c in pk_columns])
            if cur.fetchone() is not None:
                raise ConcurrentModification(
                    f"{table} row with pk {{{', '.join(pk_columns)}}} already "
                    f"exists; the delete did not land or the row was recreated. "
                    f"Refusing to reinsert."
                )
            cur.execute(
                f"INSERT INTO {tbl} ({col_sql}) VALUES ({placeholders})",
                [row[c] for c in cols],
            )
        conn.commit()
    finally:
        conn.close()
    logger.info("reinserted %s row (%d column(s), driver=%s)", table, len(cols), driver)
    return {"table": table, "reinserted": True}


async def delete_row(
    ctx,
    *,
    pool,
    table: str,
    pk: dict,
    credential_ref: str,
) -> dict:
    """Delete a row inside a saga, capturing the whole row first so the inverse
    is a re-insert. `pk` maps primary-key column(s) to value(s); compound keys
    are supported.

    The snapshot is taken before the DELETE and held in a closure, so the row is
    reversible even on an UNKNOWN outcome (a timed-out DELETE). The reinsert
    guard makes that safe: if the delete never landed, the row still exists and
    the guard refuses rather than duplicating it.
    """
    if not pk:
        raise ValueError("pk must not be empty")

    tbl = _qualified(table)
    where_sql = " AND ".join(f"{_ident(c, kind='column')} = ${i + 1}"
                             for i, c in enumerate(pk))
    pk_values = list(pk.values())
    captured: dict = {}

    async def _forward():
        async with pool.acquire() as conn:
            row = await conn.fetchrow(f"SELECT * FROM {tbl} WHERE {where_sql}", *pk_values)
            if row is None:
                raise LookupError(f"no row in {table} where pk={pk!r}")
            captured["row"] = dict(row)   # before the delete, for UNKNOWN safety
            await conn.execute(f"DELETE FROM {tbl} WHERE {where_sql}", *pk_values)
            return dict(row)

    def _compensate(row_snapshot: Any) -> Optional[Compensation]:
        row = row_snapshot if row_snapshot is not None else captured.get("row")
        if row is None:
            logger.error(
                "delete from %s (pk=%r) had an UNKNOWN outcome before the row "
                "could be read; cannot reinsert. Reconcile by hand.", table, pk)
            return None

        kwargs = {"table": table, "row": row, "pk_columns": list(pk),
                  "credential_ref": credential_ref}
        assert_no_secrets(kwargs, where="postgres.delete_row")
        return Compensation(
            fn=reinsert_row,
            handler="postgres.reinsert_row",
            kwargs=kwargs,
            description=f"reinsert deleted {table} row pk={pk!r}",
        )

    return await ctx.execute(
        tool="postgres.delete_row",
        semantics=ActionSemantics.COMPENSABLE,
        forward=_forward,
        compensate=_compensate,
        policy_args={"table": table, "pk": pk},
    )


__all__ = [
    "update_row", "restore_row",
    "insert_row", "delete_inserted_row",
    "delete_row", "reinsert_row",
    "ConcurrentModification",
]
