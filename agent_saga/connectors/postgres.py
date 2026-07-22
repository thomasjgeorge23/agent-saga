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
import os
import re
from typing import Any, Optional, Sequence

from ..reconcile import Observation, reconciler
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


# ---------------------------------------------------------------------------
# Driver dispatch: async-native when asyncpg is available, else the sync driver
# on the bounded tool pool.
#
# The two drivers disagree on placeholder syntax ($1 vs %s), so every statement
# below is built once by a helper that takes the style. Writing each query twice
# would be the obvious way to introduce a divergence between the path that is
# tested and the path that runs in production.
# ---------------------------------------------------------------------------

def _ph(style: str, index: int) -> str:
    """Positional placeholder for the driver in use. `index` is 1-based."""
    return f"${index}" if style == "asyncpg" else "%s"


_DRIVER_PREFERENCE = os.environ.get("AGENT_SAGA_PG_DRIVER", "auto").lower()
"""Which driver the compensators use: 'auto' (asyncpg when importable, else the
sync driver), 'asyncpg', or 'sync'.

Explicit because 'auto' alone is a footgun: asyncpg is a common *transitive*
dependency, so installing an unrelated package could silently change which
driver runs your rollbacks. Anyone who cares can pin it, via
AGENT_SAGA_PG_DRIVER or set_pg_driver()."""


def set_pg_driver(preference: str) -> None:
    global _DRIVER_PREFERENCE
    if preference not in ("auto", "asyncpg", "sync"):
        raise ValueError("preference must be 'auto', 'asyncpg' or 'sync'")
    _DRIVER_PREFERENCE = preference


def get_pg_driver() -> str:
    return _DRIVER_PREFERENCE


def _asyncpg_or_none():
    if _DRIVER_PREFERENCE == "sync":
        return None
    try:
        import asyncpg  # noqa: F401

        return asyncpg
    except ImportError:
        if _DRIVER_PREFERENCE == "asyncpg":
            raise
        return None


def _rowcount(status: str) -> int:
    """asyncpg returns a command tag such as 'UPDATE 3'; the count is the tail."""
    try:
        return int(str(status).strip().rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0


async def _run(credential_ref: str, plan) -> Any:
    """Execute `plan` against Postgres on whichever driver is installed.

    `plan(style, execute, fetchone)` is called with helpers bound to a single
    connection, so a check-then-write (reinsert's existence guard) stays inside
    one connection and cannot interleave with another writer.
    """
    dsn = resolve_credential(credential_ref)
    asyncpg = _asyncpg_or_none()

    if asyncpg is not None:
        conn = await asyncpg.connect(dsn)
        try:
            async def execute(sql, params):
                return _rowcount(await conn.execute(sql, *params))

            async def fetchone(sql, params):
                return await conn.fetchrow(sql, *params)

            return await plan("asyncpg", execute, fetchone), "asyncpg"
        finally:
            await conn.close()

    # No asyncpg: run the blocking driver on the bounded tool pool, which is
    # isolated from the WAL flusher so a slow rollback cannot stall fsyncs.
    from ..executors import get_tool_executor

    def _sync_plan():
        conn, driver = _connect(credential_ref)
        try:
            with conn.cursor() as cur:
                def execute(sql, params):
                    cur.execute(sql, list(params))
                    return cur.rowcount

                def fetchone(sql, params):
                    cur.execute(sql, list(params))
                    return cur.fetchone()

                result = _drive_sync(plan, execute, fetchone)
            conn.commit()
            return result, driver
        finally:
            conn.close()

    return await get_tool_executor().run(_sync_plan)


def _drive_sync(plan, execute, fetchone):
    """Run an async `plan` whose awaits are all our own sync helpers.

    The plan is written once, in async form, for both drivers. On the sync path
    its awaits resolve immediately, so stepping the coroutine by hand runs it to
    completion without an event loop -- and without a second copy of the plan.
    """
    async def _sync_execute(sql, params):
        return execute(sql, params)

    async def _sync_fetchone(sql, params):
        return fetchone(sql, params)

    coro = plan("psycopg", _sync_execute, _sync_fetchone)
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise RuntimeError(
        "postgres plan awaited something other than its own driver helpers; "
        "plans must only await execute()/fetchone()"
    )


@compensator("postgres.restore_row")
async def restore_row(
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
    guard_cols = list(expected_current)

    async def plan(style, execute, _fetchone):
        n = 0
        set_parts = []
        for c in cols:
            n += 1
            set_parts.append(f"{_ident(c, kind='column')} = {_ph(style, n)}")
        n += 1
        pk_ph = _ph(style, n)
        guard_parts = []
        for c in guard_cols:
            n += 1
            guard_parts.append(
                f"{_ident(c, kind='column')} IS NOT DISTINCT FROM {_ph(style, n)}")

        sql = (f"UPDATE {tbl} SET {', '.join(set_parts)} "
               f"WHERE {pk} = {pk_ph} AND {' AND '.join(guard_parts)}")
        params = ([previous_state[c] for c in cols] + [pk_value] +
                  [expected_current[c] for c in guard_cols])
        return await execute(sql, params)

    affected, driver = await _run(credential_ref, plan)

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
async def delete_inserted_row(table: str, pk: dict, credential_ref: str) -> dict:
    """Undo an insert by deleting the row we created. Idempotent: zero rows
    affected means it is already gone, which is success, not failure."""
    tbl = _qualified(table)
    pk_cols = list(pk)

    async def plan(style, execute, _fetchone):
        where = " AND ".join(
            f"{_ident(c, kind='column')} = {_ph(style, i)}"
            for i, c in enumerate(pk_cols, start=1))
        return await execute(f"DELETE FROM {tbl} WHERE {where}",
                             [pk[c] for c in pk_cols])

    affected, driver = await _run(credential_ref, plan)
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
async def reinsert_row(table: str, row: dict, pk_columns: list, credential_ref: str) -> dict:
    """Undo a delete by re-inserting the captured row -- unless a row with the
    same key already exists, which means the delete never landed (UNKNOWN) or
    someone recreated it. Reinserting then would either duplicate or overwrite,
    so we refuse and escalate.

    The existence check and the insert share one connection, so no other writer
    can slip between them.
    """
    tbl = _qualified(table)
    cols = list(row)
    pk_cols = list(pk_columns)

    async def plan(style, execute, fetchone):
        where = " AND ".join(
            f"{_ident(c, kind='column')} = {_ph(style, i)}"
            for i, c in enumerate(pk_cols, start=1))
        existing = await fetchone(f"SELECT 1 FROM {tbl} WHERE {where}",
                                  [row[c] for c in pk_cols])
        if existing is not None:
            return "exists"

        col_sql = ", ".join(_ident(c, kind="column") for c in cols)
        placeholders = ", ".join(_ph(style, i) for i in range(1, len(cols) + 1))
        await execute(f"INSERT INTO {tbl} ({col_sql}) VALUES ({placeholders})",
                      [row[c] for c in cols])
        return "inserted"

    outcome, driver = await _run(credential_ref, plan)
    if outcome == "exists":
        raise ConcurrentModification(
            f"{table} row with pk {{{', '.join(pk_cols)}}} already exists; the "
            f"delete did not land or the row was recreated. Refusing to reinsert."
        )
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
    "set_pg_driver", "get_pg_driver",
    "update_row", "restore_row",
    "insert_row", "delete_inserted_row",
    "delete_row", "reinsert_row",
    "ConcurrentModification",
    "observe_restored_row", "observe_deleted_insert", "observe_reinserted_row",
]


# ---------------------------------------------------------------------------
# Reconcilers: ask the database what is actually in the row
# ---------------------------------------------------------------------------
#
# A compensation returning without raising means the UPDATE reported a row
# count. It does not mean the row now holds what we intended: a trigger can
# rewrite it, a replica can be read instead of the primary, and a later writer
# can undo the undo. These re-read and compare.


async def _fetch_row(credential_ref: str, table: str, where: dict,
                     columns: Sequence[str]) -> Optional[dict]:
    tbl = _qualified(table)
    cols = [_ident(c, kind="column") for c in columns] or ["*"]
    keys = list(where)

    async def plan(style, _execute, fetchone):
        conds = " AND ".join(
            f"{_ident(c, kind='column')} = {_ph(style, i + 1)}"
            for i, c in enumerate(keys))
        sql = f"SELECT {', '.join(cols)} FROM {tbl} WHERE {conds}"
        row = await fetchone(sql, [where[c] for c in keys])
        return dict(row) if row is not None else None

    return await _run(credential_ref, plan)


def _matches(row: dict, expected: dict) -> bool:
    return all(row.get(k) == v for k, v in expected.items())


@reconciler("postgres.restore_row")
async def observe_restored_row(*, table: str, pk_column: str, pk_value: Any,
                               previous_state: dict, expected_current: dict,
                               credential_ref: str, **_ignored) -> Observation:
    """Did the row actually go back to what it was before the agent touched it?

    Three outcomes worth distinguishing, and a boolean would collapse them:
    the row holds the previous values (reversed), it still holds what the agent
    wrote (the rollback did not take), or it holds neither -- someone else has
    written since, and no automated answer is safe.
    """
    columns = sorted(set(previous_state) | set(expected_current))
    row = await _fetch_row(credential_ref, table, {pk_column: pk_value}, columns)
    if row is None:
        return Observation(exists=False, reversed_=False,
                           detail=f"row {pk_column}={pk_value!r} no longer exists")
    if _matches(row, previous_state):
        return Observation(exists=True, reversed_=True,
                           detail="row holds its pre-saga values")
    if _matches(row, expected_current):
        return Observation(exists=True, reversed_=False,
                           detail="row still holds the values the agent wrote")
    return Observation(
        exists=True, reversed_=None,
        detail=("row matches neither the pre-saga nor the agent-written state; "
                "a third party has written to it since"))


@reconciler("postgres.delete_inserted_row")
async def observe_deleted_insert(*, table: str, pk: dict, credential_ref: str,
                                 **_ignored) -> Observation:
    """The inverse of an INSERT is a DELETE, so reversal means the row is gone."""
    row = await _fetch_row(credential_ref, table, pk, list(pk))
    return Observation(
        exists=row is not None, reversed_=row is None,
        detail=("inserted row was removed" if row is None
                else "inserted row is still present"))


@reconciler("postgres.reinsert_row")
async def observe_reinserted_row(*, table: str, row: dict, pk_columns: list,
                                 credential_ref: str, **_ignored) -> Observation:
    """The inverse of a DELETE is a re-INSERT, so reversal means it is back --
    and back with the values it had, not merely a row with the same key."""
    where = {c: row[c] for c in pk_columns if c in row}
    found = await _fetch_row(credential_ref, table, where, sorted(row))
    if found is None:
        return Observation(exists=False, reversed_=False,
                           detail="deleted row has not been restored")
    if _matches(found, row):
        return Observation(exists=True, reversed_=True,
                           detail="deleted row is back with its original values")
    return Observation(
        exists=True, reversed_=None,
        detail="a row with that key exists but its values differ from the original")
