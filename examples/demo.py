"""The 30-second demo: an agent breaks three systems, then unbreaks them.

Runs with no credentials, no database, and no network -- the connectors are
pointed at in-memory fakes so anyone can `python examples/demo.py` straight
after cloning.

    python examples/demo.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_saga import (  # noqa: E402
    ActionSemantics,
    AsyncWAL,
    Compensation,
    PreFlightGate,
    PreFlightViolation,
    RecoveryDaemon,
    Resolution,
    Rule,
    SagaContext,
    Verdict,
    arg_exceeds,
    compensator,
    semantics_is,
)

logging.basicConfig(level=logging.ERROR, format="%(message)s")

BOLD, DIM, RED, GREEN, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m")


# --------------------------------------------------------------------------
# A tiny fake world so the demo is self-contained.
# --------------------------------------------------------------------------

WORLD = {"crm": {"acct_42": {"status": "prospect"}},
         "ledger": [], "inbox": []}


def show(title: str) -> None:
    print(f"\n{DIM}{'-' * 68}{RESET}\n{BOLD}{title}{RESET}\n")


def world() -> str:
    return (f"    crm[acct_42].status = {WORLD['crm']['acct_42']['status']!r}\n"
            f"    ledger              = {WORLD['ledger']}\n"
            f"    inbox               = {WORLD['inbox']}")


@compensator("demo.restore_crm")
def restore_crm(account_id: str, previous: dict, expected_current: dict):
    if WORLD["crm"][account_id] != expected_current:
        raise RuntimeError("record changed since we wrote it; refusing to clobber")
    WORLD["crm"][account_id] = dict(previous)


@compensator("demo.refund")
def refund(charge_id: str, amount: int, idempotency_key: str):
    if any(e.get("refunds") == charge_id for e in WORLD["ledger"]):
        return  # idempotent: the daemon may retry
    WORLD["ledger"].append({"id": f"re_{charge_id}", "refunds": charge_id,
                            "amount": -amount})


async def update_crm(ctx, account_id: str, updates: dict):
    def _forward():
        before = dict(WORLD["crm"][account_id])
        WORLD["crm"][account_id].update(updates)
        return before

    return await ctx.execute(
        tool="crm.update", semantics=ActionSemantics.COMPENSABLE, forward=_forward,
        compensate=lambda before: Compensation(
            fn=restore_crm, handler="demo.restore_crm",
            kwargs={"account_id": account_id, "previous": before,
                    "expected_current": {**before, **updates}},
            description=f"restore {account_id}"))


async def charge_card(ctx, customer: str, amount: int):
    def _forward():
        charge = {"id": f"ch_{len(WORLD['ledger']) + 1}", "amount": amount,
                  "customer": customer}
        WORLD["ledger"].append(charge)
        return charge

    return await ctx.execute(
        tool="stripe.charge", semantics=ActionSemantics.COMPENSABLE, forward=_forward,
        compensate=lambda ch: Compensation(
            fn=refund, handler="demo.refund",
            kwargs={"charge_id": ch["id"], "amount": ch["amount"],
                    "idempotency_key": f"refund-{ch['id']}"},
            description=f"refund {ch['id']}"),
        policy_args={"amount": amount, "customer": customer})


async def send_email(ctx, to: str, body: str):
    return await ctx.execute(
        tool="email.send", semantics=ActionSemantics.IRREVERSIBLE,
        forward=lambda: WORLD["inbox"].append({"to": to, "body": body}))


# --------------------------------------------------------------------------

async def scene_1_rollback(tmp: Path) -> None:
    show("1. The agent hallucinates mid-transaction. Everything unwinds.")
    print(f"{DIM}before{RESET}\n{world()}")

    wal = AsyncWAL(tmp / "scene1.jsonl")
    await wal.start()
    ctx = SagaContext(wal=wal)
    await ctx.begin()

    try:
        await update_crm(ctx, "acct_42", {"status": "customer"})
        await charge_card(ctx, "cus_7", 49900)
        print(f"\n{DIM}mid-saga (two real side effects have landed){RESET}\n{world()}")
        raise ValueError("model invented a field: 'contract_signed_at'")
    except ValueError as exc:
        print(f"\n{RED}    ✗ {exc}{RESET}")
        report = await ctx.rollback()
        await ctx.finish(aborted=True, clean=report.clean)

    print(f"\n{DIM}after rollback{RESET}\n{world()}")
    print(f"\n{GREEN}    ✓ {report.summary()}{RESET}")
    print(f"{DIM}      compensated LIFO: {' -> '.join(s.tool for s in report.compensated)}{RESET}")
    await wal.close()


async def scene_2_gate(tmp: Path) -> None:
    show("2. The gate refuses what cannot be undone -- before it happens.")

    wal = AsyncWAL(tmp / "scene2.jsonl")
    await wal.start()
    gate = PreFlightGate(rules=[
        Rule("no-large-charges", arg_exceeds("amount", 100_000), Verdict.BLOCK,
             "Charge exceeds the autonomous limit."),
        Rule("irreversible-needs-human", semantics_is(ActionSemantics.IRREVERSIBLE),
             Verdict.REQUIRE_APPROVAL, "Cannot be undone or compensated."),
    ])
    ctx = SagaContext(gate=gate, wal=wal)
    await ctx.begin()

    for label, coro in (
        ("charge $2,500.00", charge_card(ctx, "cus_7", 250_000)),
        ("email the customer", send_email(ctx, "cfo@acme.com", "Your contract is signed.")),
    ):
        try:
            await coro
            print(f"{GREEN}    ✓ {label} -- allowed{RESET}")
        except PreFlightViolation as exc:
            print(f"{YELLOW}    ⛔ {label}{RESET}\n{DIM}       {exc}{RESET}")

    print(f"\n{DIM}    inbox is still {WORLD['inbox']} -- nothing was sent, "
          f"so nothing needs undoing.{RESET}")
    await ctx.finish()
    await wal.close()


async def scene_3_crash(tmp: Path) -> None:
    show("3. The process dies mid-saga. A daemon finishes the job.")

    wal = AsyncWAL(tmp / "scene3.jsonl")
    await wal.start()
    ctx = SagaContext(wal=wal, lease_ttl=0.2)
    await ctx.begin()
    await charge_card(ctx, "cus_9", 12500)
    print(f"{DIM}    a charge landed, then the process was killed:{RESET}")
    print(f"    ledger = {WORLD['ledger']}")
    # Simulate SIGKILL: stop renewing the lease, never roll back, never complete.
    await wal.close()

    print(f"\n{DIM}    ...lease expires; saga-recoveryd sweeps the WAL...{RESET}")
    await asyncio.sleep(0.6)

    outcomes = await RecoveryDaemon(tmp / "scene3.jsonl").recover_all()
    for o in outcomes:
        colour = GREEN if o.resolution is Resolution.RECOVERED else YELLOW
        print(f"{colour}    ✓ saga {o.saga_id[:8]} -> {o.resolution.value}"
              f" ({', '.join(o.compensated) or o.reason}){RESET}")
    print(f"\n    ledger = {WORLD['ledger']}")
    print(f"{DIM}    the refund was issued by a different process, from the WAL alone.{RESET}")


async def main() -> None:
    print(f"\n{BOLD}agent-saga{RESET} {DIM}-- the undo button for AI agents{RESET}")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        await scene_1_rollback(tmp)
        await scene_2_gate(tmp)
        await scene_3_crash(tmp)
    print(f"\n{DIM}{'-' * 68}{RESET}")
    print(f"{DIM}Nothing above touched a real system. "
          f"Swap the fakes for agent_saga.connectors.*{RESET}\n")


if __name__ == "__main__":
    asyncio.run(main())
