"""An agent that dies at a chosen moment, against a durable external system.

`os._exit()` skips atexit hooks, finally blocks and event-loop shutdown -- the
closest portable approximation of SIGKILL or an OOM kill. Nothing in agent_saga
gets to clean up, which is the entire point: every guarantee this suite checks
has to hold with no cooperation from the dying process.

Usage: python chaos_worker.py <wal_path> <ledger_path> <mode> [n_steps]

Crash points, chosen because each one leaves the log in a structurally
different state:

  after_intent      the intent is fsynced; the charge has NOT happened
  after_effect      the charge HAS happened; its compensation is not yet durable
  after_commit      the compensation descriptor is durable
  mid_compensation  died half way through unwinding a multi-step saga
  clean             no crash; used as the control
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from chaos_ledger import Ledger  # noqa: E402

from agent_saga import (  # noqa: E402
    ActionSemantics,
    AsyncWAL,
    Compensation,
    PreFlightGate,
    SagaContext,
    compensator,
)

LEDGER_PATH = os.environ.get("CHAOS_LEDGER", "")


@compensator("chaos.refund")
def refund(charge_id, ledger_path, idempotency_key=""):
    """Named, JSON-serializable: a closure could not cross the process
    boundary into the recovery daemon."""
    Ledger(ledger_path).refund(charge_id, idempotency_key=idempotency_key)


async def main(wal_path: str, ledger_path: str, mode: str, steps: int) -> None:
    ledger = Ledger(ledger_path)
    wal = AsyncWAL(wal_path)
    await wal.start()
    ctx = SagaContext(gate=PreFlightGate(approval_provider=lambda c, r: True),
                      wal=wal, lease_ttl=0.3)
    await ctx.begin()

    def make_charge(index: int):
        def _charge():
            if mode == "after_intent" and index == 0:
                # The intent is already durable and fsynced. Die before the
                # money moves: the log now claims something that never happened.
                os._exit(1)
            result = ledger.charge(f"ch_{index}", 100 * (index + 1))
            if mode == "after_effect" and index == 0:
                # The charge is real and the compensation descriptor is not yet
                # on disk. This is the genuinely dangerous window.
                os._exit(1)
            return result

        return _charge

    for index in range(steps):
        await ctx.execute(
            tool=f"chaos.charge.{index}",
            semantics=ActionSemantics.COMPENSABLE,
            forward=make_charge(index),
            forward_kwargs={},
            policy_args={"amount": 100 * (index + 1)},
            compensate=lambda r, p=ledger_path: Compensation(
                fn=refund, handler="chaos.refund",
                kwargs={"charge_id": r["id"], "ledger_path": p}),
        )
        if mode == "after_commit" and index == 0:
            os._exit(1)

    if mode == "mid_compensation":
        # Unwind by hand so we can die between compensations.
        ctx.record_abort(RuntimeError("chaos"))
        for position, step in enumerate(reversed(ctx.stack)):
            if step.compensation is not None:
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda s=step: s.compensation.fn(**s.compensation.kwargs))
                wal.append("COMPENSATED", {
                    "saga_id": ctx.saga_id, "step_id": step.step_id,
                    "tool": step.tool,
                    "idempotency_key": step.compensation.idempotency_key})
                await wal.barrier()
            if position == 0:
                os._exit(1)          # one down, the rest still standing

    await ctx.finish()
    await wal.close()


if __name__ == "__main__":
    wal_path, ledger_path, mode = sys.argv[1], sys.argv[2], sys.argv[3]
    steps = int(sys.argv[4]) if len(sys.argv) > 4 else 1
    asyncio.run(main(wal_path, ledger_path, mode, steps))
