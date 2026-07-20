"""Subprocess that performs a real side effect and then dies without cleanup.

`os._exit()` skips atexit hooks, finally blocks, and the event loop shutdown --
the closest portable approximation of SIGKILL/OOM. Nothing in agent_saga gets a
chance to roll back, which is the entire point.

Usage: python crash_worker.py <wal_path> <effects_path> <mode>
  mode = commit      -- crash after the effect is committed and durable
  mode = irreversible-- crash after an approved IRREVERSIBLE effect
  mode = closure     -- crash after a step whose compensation is a closure
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_saga import (  # noqa: E402
    ActionSemantics,
    AsyncWAL,
    Compensation,
    PreFlightGate,
    SagaContext,
    compensator,
)


@compensator("test.refund")
def refund(charge_id, effects_path):
    with open(effects_path, "a", encoding="utf-8") as fh:
        fh.write(f"refunded:{charge_id}\n")


async def main(wal_path: str, effects_path: str, mode: str) -> None:
    wal = AsyncWAL(wal_path)
    await wal.start()
    gate = PreFlightGate(approval_provider=lambda ctx, rule: True)
    ctx = SagaContext(gate=gate, wal=wal, lease_ttl=0.3)
    await ctx.begin()

    def charge():
        with open(effects_path, "a", encoding="utf-8") as fh:
            fh.write("charged:ch_crash_1\n")
        return {"charge_id": "ch_crash_1"}

    if mode == "closure":
        # Deliberately unrecoverable: a lambda cannot cross a process boundary.
        comp = lambda r: Compensation(fn=lambda: None, description="in-process only")
        semantics = ActionSemantics.COMPENSABLE
    elif mode == "irreversible":
        comp = None
        semantics = ActionSemantics.IRREVERSIBLE
    else:
        comp = lambda r: Compensation(
            fn=refund, handler="test.refund",
            kwargs={"charge_id": r["charge_id"], "effects_path": effects_path},
            idempotency_key="idem-crash-1",
        )
        semantics = ActionSemantics.COMPENSABLE

    await ctx.execute(tool="stripe.charge", semantics=semantics,
                      forward=charge, compensate=comp)

    # Die. No rollback, no SAGA_COMPLETE, no lease renewal.
    os._exit(9)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3]))
