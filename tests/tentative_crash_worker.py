"""Registers a durable tentative resource, applies a provisional debit, then dies.

`os._exit()` skips atexit hooks, finally blocks and the event loop, so nothing
in agent_saga gets a chance to resolve the resource. That is the point: the
recovery daemon must be able to settle it from the WAL alone.

Usage: python tentative_crash_worker.py <wal> <effects> <account>
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_saga import AsyncWAL, SagaContext  # noqa: E402
from agent_saga.patterns import TentativeResource  # noqa: E402
from agent_saga.registry import compensator  # noqa: E402


@compensator("demo.restore_balance")
def restore_balance(account, path, idempotency_key=None):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"restored:{account}\n")


async def main(wal_path: str, effects: str, account: str) -> None:
    wal = AsyncWAL(wal_path)
    await wal.start()
    ctx = SagaContext(wal=wal, lease_ttl=0.3)
    await ctx.begin()

    # Durable registration: on disk BEFORE the debit, so a crash in between
    # still leaves a record that this resource is provisional.
    await ctx.register_tentative_durable(TentativeResource(
        resource_id=account,
        saga_id=ctx.saga_id,
        rollback_handler="demo.restore_balance",
        rollback_kwargs={"account": account, "path": effects},
    ))

    with open(effects, "a", encoding="utf-8") as fh:
        fh.write(f"debited:{account}\n")

    os._exit(9)   # nothing unwinds


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3]))
