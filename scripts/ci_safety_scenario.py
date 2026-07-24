"""Produce a WAL that exercises the engine's real safety paths, for CI to audit.

The unit tests prove each piece in isolation. This runs the engine end to end --
a saga that succeeds, and one that fails and rolls back -- so `agent-saga verify`
and `agent-saga certify` can assert the *whole-system* property that unit tests
cannot: after a real failure, the log contains no effect the engine cannot
account for.

Usage:  python scripts/ci_safety_scenario.py <wal-path>
"""

from __future__ import annotations

import asyncio
import sys

from agent_saga import ActionSemantics, PreFlightGate
from agent_saga.context import Compensation, SagaContext
from agent_saga.wal.file_wal import FileWAL

WORLD: dict = {"charges": [], "rows": {}}


def _charge(amount: int) -> dict:
    charge = {"id": f"ch_{len(WORLD['charges']) + 1}", "amount": amount}
    WORLD["charges"].append(charge)
    return charge


def _refund(charge_id: str, **_) -> None:
    WORLD["charges"] = [c for c in WORLD["charges"] if c["id"] != charge_id]


def _write_row(key: str, value: str) -> dict:
    before = WORLD["rows"].get(key)
    WORLD["rows"][key] = value
    return {"key": key, "before": before}


def _restore_row(key: str, before, **_) -> None:
    if before is None:
        WORLD["rows"].pop(key, None)
    else:
        WORLD["rows"][key] = before


async def _happy_path(wal: FileWAL) -> None:
    ctx = SagaContext(wal=wal, gate=PreFlightGate())
    await ctx.begin()
    await ctx.execute(
        tool="stripe.charge", semantics=ActionSemantics.COMPENSABLE,
        forward=lambda: _charge(1000),
        compensate=lambda r: Compensation(
            fn=_refund, handler="stripe.refund",
            kwargs={"charge_id": r["id"]}, description="refund"))
    await ctx.execute(
        tool="db.write", semantics=ActionSemantics.COMPENSABLE,
        forward=lambda: _write_row("acct-1", "active"),
        compensate=lambda r: Compensation(
            fn=_restore_row, handler="db.restore",
            kwargs={"key": r["key"], "before": r["before"]}, description="restore"))
    await ctx.finish()


async def _failure_path(wal: FileWAL) -> None:
    """Commits two compensable effects, then fails. A clean rollback must leave
    the world exactly as it found it -- and the log must show it."""
    ctx = SagaContext(wal=wal, gate=PreFlightGate())
    await ctx.begin()
    try:
        await ctx.execute(
            tool="stripe.charge", semantics=ActionSemantics.COMPENSABLE,
            forward=lambda: _charge(4200),
            compensate=lambda r: Compensation(
                fn=_refund, handler="stripe.refund",
                kwargs={"charge_id": r["id"]}, description="refund"))
        await ctx.execute(
            tool="db.write", semantics=ActionSemantics.COMPENSABLE,
            forward=lambda: _write_row("acct-2", "provisioned"),
            compensate=lambda r: Compensation(
                fn=_restore_row, handler="db.restore",
                kwargs={"key": r["key"], "before": r["before"]}, description="restore"))
        raise RuntimeError("downstream reconciliation mismatch")
    except BaseException:
        report = await ctx.rollback()
        await ctx.finish(aborted=True, clean=report.clean)
        if not report.clean:
            raise SystemExit("rollback was not clean -- the engine stranded an effect")


async def main(path: str) -> int:
    wal = FileWAL(path)
    await wal.start()
    try:
        await _happy_path(wal)
        await _failure_path(wal)
    finally:
        await wal.close()

    # The world must be back to exactly what the happy path left behind: the
    # failed saga's charge refunded and its row restored.
    charge_ids = [c["id"] for c in WORLD["charges"]]
    if len(WORLD["charges"]) != 1:
        print(f"FAIL: expected 1 surviving charge, found {charge_ids}", file=sys.stderr)
        return 1
    if "acct-2" in WORLD["rows"]:
        print("FAIL: rolled-back saga left acct-2 behind", file=sys.stderr)
        return 1

    print(f"scenario complete: {path}")
    print(f"  surviving charges : {charge_ids}")
    print(f"  surviving rows    : {sorted(WORLD['rows'])}")
    return 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "./ci-scenario.wal"
    raise SystemExit(asyncio.run(main(target)))
