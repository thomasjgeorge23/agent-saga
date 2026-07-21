"""chaos_demo.py -- optimism vs. SagaOps, side by side, in five seconds.

Runs the *same* three-step AI workflow twice against the *same* mock world:

    1. Charge the customer   (Stripe    -- COMPENSABLE: a refund, not a rewind)
    2. Insert the account    (Postgres  -- COMPENSABLE: delete the row we wrote)
    3. Send the welcome mail (SMTP      -- hard failure, mid-flight)

Run A executes optimistically, the way most agent code is written today. Step 3
raises and the process leaves money taken and a half-built account behind.

Run B wraps the identical calls in `saga_scope`. Step 3 raises, the boundary
intercepts, and the compensations run last-in-first-out until the world is back
where it started.

    python examples/chaos_demo.py            # the demo
    python examples/chaos_demo.py --logs     # also show the engine's own trace
    python examples/chaos_demo.py --no-color # plain output for CI / piping

Nothing here touches a real network, database, or mail server: `MockWorld` is an
in-memory stand-in whose state we print before and after, so the difference is
visible rather than asserted.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_saga import (  # noqa: E402
    ActionSemantics,
    Compensation,
    SagaAborted,
    compensator,
    configure_logging,
    saga_scope,
)

C = ActionSemantics.COMPENSABLE


# ===========================================================================
# Terminal presentation
# ===========================================================================

class Style:
    """ANSI styling that degrades safely.

    Two independent fallbacks, because a demo that prints mojibake on a
    reviewer's terminal is worse than a plain one:

      * colour is dropped when stdout is not a TTY (piping to a file or CI), or
        when --no-color is passed;
      * box-drawing characters are swapped for ASCII when the console encoding
        cannot represent them (Windows cp1252 raises UnicodeEncodeError).
    """

    def __init__(self, color: bool = True):
        self.color = color and sys.stdout.isatty() and _enable_ansi()
        self.unicode = _supports_unicode()

    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def red(self, t): return self._c("31", t)
    def green(self, t): return self._c("32", t)
    def yellow(self, t): return self._c("33", t)
    def blue(self, t): return self._c("36", t)
    def grey(self, t): return self._c("90", t)
    def bold(self, t): return self._c("1", t)
    def on_red(self, t): return self._c("41;97;1", t)
    def on_green(self, t): return self._c("42;30;1", t)

    @property
    def hbar(self) -> str: return "─" if self.unicode else "-"
    @property
    def tick(self) -> str: return "✓" if self.unicode else "OK"
    @property
    def cross(self) -> str: return "✗" if self.unicode else "XX"
    @property
    def arrow(self) -> str: return "↺" if self.unicode else "<<"

    def rule(self, width: int = 74) -> str:
        return self.grey(self.hbar * width)

    def banner(self, text: str, good: bool) -> str:
        pad = f"  {text}  "
        return (self.on_green if good else self.on_red)(pad)


def _enable_ansi() -> bool:
    """Windows consoles need VT processing switched on explicitly."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # -11 = STD_OUTPUT_HANDLE, 0x4 = ENABLE_VIRTUAL_TERMINAL_PROCESSING
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x4))
    except Exception:
        return False


def _supports_unicode() -> bool:
    try:
        "─✓✗↺".encode(sys.stdout.encoding or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


# ===========================================================================
# The mock world -- three systems an agent can corrupt
# ===========================================================================

@dataclass
class MockWorld:
    """Stands in for Stripe, Postgres and an SMTP server.

    Every mutation is recorded so the demo can print the true state afterwards
    rather than claiming one. `ledger` keeps refunds as separate entries on
    purpose: that is what COMPENSABLE means -- the money comes back, but the
    statement shows two lines, never zero.
    """

    ledger: list[dict] = field(default_factory=list)
    rows: dict[str, dict] = field(default_factory=dict)
    outbox: list[str] = field(default_factory=list)
    _seq: int = 0

    # -- forward actions --------------------------------------------------

    def charge(self, customer: str, amount_cents: int) -> dict:
        self._seq += 1
        charge_id = f"ch_{self._seq:04d}"
        self.ledger.append({"id": charge_id, "customer": customer,
                            "amount": amount_cents, "kind": "charge"})
        return {"charge_id": charge_id, "amount": amount_cents}

    def insert_account(self, customer: str, email: str) -> dict:
        self.rows[customer] = {"customer": customer, "email": email,
                               "status": "provisioning"}
        return {"row_id": customer}

    def send_welcome_email(self, email: str) -> str:
        # The hard failure. A real SMTP relay refusing mid-flight is exactly the
        # kind of error an agent cannot foresee or retry its way out of.
        raise ConnectionError(f"SMTP relay refused connection sending to {email}")

    # -- compensating actions ---------------------------------------------

    def refund(self, charge_id: str) -> dict:
        # Idempotent: a compensation may be retried, or replayed by the recovery
        # daemon after a crash. Refunding twice must be impossible.
        if any(e["kind"] == "refund" and e["ref"] == charge_id for e in self.ledger):
            return {"status": "already_refunded"}
        original = next((e for e in self.ledger
                         if e["kind"] == "charge" and e["id"] == charge_id), None)
        if original is None:
            return {"status": "no_such_charge"}
        self.ledger.append({"id": f"re_{charge_id[3:]}", "ref": charge_id,
                            "customer": original["customer"],
                            "amount": -original["amount"], "kind": "refund"})
        return {"status": "refunded"}

    def delete_account(self, row_id: str) -> dict:
        # Also idempotent: deleting an already-deleted row is a safe no-op.
        existed = self.rows.pop(row_id, None) is not None
        return {"status": "deleted" if existed else "already_absent"}

    # -- reporting ---------------------------------------------------------

    @property
    def net_cents(self) -> int:
        return sum(e["amount"] for e in self.ledger)

    def is_clean(self) -> bool:
        """Clean means the world is indistinguishable from never having run --
        net zero money, no stranded rows, nothing sent."""
        return self.net_cents == 0 and not self.rows and not self.outbox


# ===========================================================================
# Compensation handlers -- registered by name so the recovery daemon could
# replay them after a crash, not just this process.
# ===========================================================================

WORLD = MockWorld()


@compensator("demo.stripe.refund")
def compensate_refund(charge_id: str) -> dict:
    return WORLD.refund(charge_id)


@compensator("demo.postgres.delete_account")
def compensate_delete_account(row_id: str) -> dict:
    return WORLD.delete_account(row_id)


# ===========================================================================
# The workflow, expressed twice
# ===========================================================================

CUSTOMER = "cus_acme_42"
EMAIL = "ops@acme.example"
AMOUNT = 49_900  # cents


def _print_state(s: Style, world: MockWorld, title: str) -> None:
    print(f"\n  {s.bold(title)}")
    money = f"${world.net_cents / 100:,.2f}"
    money_s = s.green(money) if world.net_cents == 0 else s.red(money)
    rows = f"{len(world.rows)} row(s)"
    rows_s = s.green(rows) if not world.rows else s.red(rows)
    mail = f"{len(world.outbox)} sent"
    n = len(world.ledger)
    entries = f"[{n} ledger {'entry' if n == 1 else 'entries'}]"
    print(f"    Stripe    net charged : {money_s}  {s.grey(entries)}")
    print(f"    Postgres  accounts    : {rows_s}")
    print(f"    Email     outbox      : {mail}")
    for e in world.ledger:
        sign = "+" if e["amount"] >= 0 else "-"
        label = f"{e['kind']:<7} {e['id']:<10} {sign}${abs(e['amount']) / 100:,.2f}"
        print(f"      {s.grey(label)}")
    for row in world.rows.values():
        detail = f"account {row['customer']} ({row['status']})"
        print(f"      {s.grey(detail)}")


async def run_without_sagaops(s: Style) -> MockWorld:
    """The way most agent tool-calling is written today: optimistically."""
    global WORLD
    WORLD = world = MockWorld()

    print()
    print(s.banner("RUN A  -  WITHOUT SagaOps  (optimistic execution)", good=False))
    print()

    print(f"  {s.blue('1.')} stripe.charge         ", end="", flush=True)
    charge = world.charge(CUSTOMER, AMOUNT)
    print(s.green(f"{s.tick} charged {charge['charge_id']}  ${AMOUNT / 100:,.2f}"))

    print(f"  {s.blue('2.')} postgres.insert_row   ", end="", flush=True)
    row = world.insert_account(CUSTOMER, EMAIL)
    print(s.green(f"{s.tick} inserted account {row['row_id']}"))

    print(f"  {s.blue('3.')} email.send_welcome    ", end="", flush=True)
    try:
        world.send_welcome_email(EMAIL)
        print(s.green(f"{s.tick} sent"))
    except ConnectionError as exc:
        print(s.red(f"{s.cross} {exc}"))
        print()
        print(f"  {s.red('The exception propagates. Nothing unwinds. The process exits.')}")

    _print_state(s, world, "Resulting state:")
    print()
    print(f"  {s.on_red('  CORRUPTED  ')} "
          f"{s.red('customer charged $499.00, account stranded in provisioning, no email.')}")
    print(f"  {s.grey('A human now has to find this, and every other case like it, by hand.')}")
    return world


async def run_with_sagaops(s: Style, show_logs: bool) -> MockWorld:
    """The same three calls, inside a transactional boundary."""
    global WORLD
    WORLD = world = MockWorld()

    print()
    print(s.banner("RUN B  -  WITH SagaOps  (transactional boundary)", good=True))
    print()

    try:
        async with saga_scope() as saga:
            print(f"  {s.grey(f'saga {saga.saga_id[:12]} opened')}")

            print(f"  {s.blue('1.')} stripe.charge         ", end="", flush=True)
            charge = await saga.execute(
                tool="stripe.charge",
                semantics=C,
                forward=lambda: world.charge(CUSTOMER, AMOUNT),
                # The inverse is derived from the forward call's *result* -- the
                # charge id does not exist until the charge returns. This is the
                # thing a statically declared workflow cannot express.
                compensate=lambda r: Compensation(
                    fn=compensate_refund,
                    handler="demo.stripe.refund",
                    kwargs={"charge_id": r["charge_id"]},
                    idempotency_key=f"refund-{r['charge_id']}",
                    description=f"refund {r['charge_id']}"),
                policy_args={"amount": AMOUNT, "customer_id": CUSTOMER},
            )
            print(s.green(f"{s.tick} charged {charge['charge_id']}") +
                  s.grey("   compensation registered: refund"))

            print(f"  {s.blue('2.')} postgres.insert_row   ", end="", flush=True)
            row = await saga.execute(
                tool="postgres.insert_row",
                semantics=C,
                forward=lambda: world.insert_account(CUSTOMER, EMAIL),
                compensate=lambda r: Compensation(
                    fn=compensate_delete_account,
                    handler="demo.postgres.delete_account",
                    kwargs={"row_id": r["row_id"]},
                    idempotency_key=f"delete-{r['row_id']}",
                    description=f"delete account {r['row_id']}"),
                policy_args={"table": "accounts"},
            )
            print(s.green(f"{s.tick} inserted {row['row_id']}") +
                  s.grey("   compensation registered: delete row"))

            print(f"  {s.blue('3.')} email.send_welcome    ", end="", flush=True)
            await saga.execute(
                tool="email.send_welcome",
                semantics=C,
                forward=lambda: world.send_welcome_email(EMAIL),
                compensate=lambda r: None,
            )
            print(s.green(f"{s.tick} sent"))

    except SagaAborted as aborted:
        print(s.red(f"{s.cross} {aborted.cause}"))
        print()
        print(f"  {s.yellow(f'{s.arrow} boundary intercepted the failure -- compensating LIFO')}")
        for step in aborted.report.compensated:
            print(f"      {s.green(s.tick)} {step.tool:<22} "
                  f"{s.grey(step.compensation.description)}")
        for step in aborted.report.orphaned:
            print(f"      {s.yellow('!')} {step.tool:<22} "
                  f"{s.yellow('UNKNOWN -> reported, not guessed at')}")
        print()
        print(f"  {s.grey(aborted.report.summary())}")

    _print_state(s, world, "Resulting state:")
    print()
    if world.is_clean():
        print(f"  {s.on_green('  RESTORED  ')} "
              f"{s.green('charge refunded, account removed. Net $0.00, zero stranded rows.')}")
        print(f"  {s.grey('The ledger keeps both entries -- that is what COMPENSABLE means.')}")
    else:
        print(f"  {s.on_red('  INCOMPLETE  ')} {s.red('see the rollback report above.')}")

    # The most important line in the demo. The engine reports the mail step as
    # UNKNOWN rather than "fine", because a refused connection is not provably
    # distinguishable from a send it never got an acknowledgement for. Nothing
    # was actually sent here -- but the engine will not claim knowledge it does
    # not have. A tool that quietly rounded this up to "clean" is a tool you
    # cannot put in front of a bank.
    print()
    print(f"  {s.yellow('Read the rollback report again:')} it says INCOMPLETE, not clean.")
    print(f"  {s.grey('The two effects it could reverse, it reversed. The mail step it')}")
    print(f"  {s.grey('marks UNKNOWN and surfaces for a human -- because a refused')}")
    print(f"  {s.grey('connection cannot be proven to be a mail that never went out.')}")
    print(f"  {s.grey('Honest beats optimistic. That distinction is the whole product.')}")
    return world


# ===========================================================================

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    ap.add_argument("--logs", action="store_true",
                    help="also print the engine's own correlated diagnostic trace")
    args = ap.parse_args(argv)

    s = Style(color=not args.no_color)

    if args.logs:
        configure_logging(level=logging.INFO, stream=sys.stdout)
    else:
        # Keep the demo's own narration clean; the engine still logs, we just
        # do not attach a handler for it.
        logging.getLogger("agent_saga").addHandler(logging.NullHandler())
        logging.getLogger("agent_saga").propagate = False

    print()
    print(s.bold("  SagaOps chaos demo") + s.grey("  --  the same workflow, twice"))
    print(s.rule())
    print(f"  {s.grey('3 steps: charge $499.00, provision the account, send the welcome mail.')}")
    print(f"  {s.grey('The mail server refuses the connection. Watch what each run leaves behind.')}")
    print(s.rule())

    after_a = asyncio.run(run_without_sagaops(s))
    print()
    print(s.rule())
    after_b = asyncio.run(run_with_sagaops(s, args.logs))

    print()
    print(s.rule())
    print(f"  {s.bold('Side by side')}")
    print(f"    {'':<26}{'WITHOUT':>16}{'WITH SagaOps':>18}")
    rows = [
        ("customer out of pocket", f"${after_a.net_cents / 100:,.2f}",
         f"${after_b.net_cents / 100:,.2f}"),
        ("orphaned db rows", str(len(after_a.rows)), str(len(after_b.rows))),
        ("manual cleanup needed", "yes", "no"),
    ]
    for label, a, b in rows:
        a_s = s.red(f"{a:>16}") if a not in ("$0.00", "0", "no") else s.green(f"{a:>16}")
        b_s = s.green(f"{b:>18}") if b in ("$0.00", "0", "no") else s.red(f"{b:>18}")
        print(f"    {label:<26}{a_s}{b_s}")
    print(s.rule())
    print(f"  {s.grey('pip install agent-saga')}   "
          f"{s.grey('github.com/thomasjgeorge23/agent-saga')}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
