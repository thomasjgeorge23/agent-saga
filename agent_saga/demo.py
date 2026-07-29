"""`agent-saga demo` -- three acts that make an invisible guarantee visible.

This library's value is invisible until something fails, which is a hard thing
to put in a README. So the demo does not describe the guarantee; it breaks
things in front of you and lets you read the world state afterwards.

    Act I    an ordinary agent fails halfway. Money is gone, a server is
             running, and nobody is coming to clean it up.
    Act II   the same calls inside a saga. Same failure, world restored.
    Act III  the same saga, but the process is KILLED mid-transaction --
             `os._exit()`, which skips finally blocks, atexit hooks and loop
             shutdown, the closest portable stand-in for SIGKILL. Then a
             SEPARATE process reads the write-ahead log and finishes the
             rollback the dead one could not.

Act III is the one that changes how people think about agent safety, because
it is the case every other answer quietly gives up on. A `try/except` stack
holds its compensations in memory and dies with the process. A checkpoint
restores your agent's state, not the world's -- it cannot un-charge a card.
Here the intent was on disk before the effect fired, so recovery does not
depend on the process that caused it still being alive.

Everything below is the real engine: a real WAL, real gate, real compensations,
real recovery daemon, a real killed subprocess. The "world" is a JSON file so
that a process which dies without cleanup still leaves its damage somewhere the
next process can see. No network, no services, no configuration.

    agent-saga demo              # ~5 seconds
    agent-saga demo --no-color   # plain text for CI or piping
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .registry import compensator
from .semantics import ActionSemantics, Compensation

__all__ = ["run_demo"]

# ASCII only. This prints to consoles whose encoding is not UTF-8 (Windows
# cp1252/cp437), where a box-drawing character or an emoji raises
# UnicodeEncodeError and turns a demo into a bug report.
_RESET = "\033[0m"
_STYLES = {
    "dim": "\033[2m", "bold": "\033[1m", "red": "\033[31m", "green": "\033[32m",
    "yellow": "\033[33m", "blue": "\033[36m", "grey": "\033[90m",
}


class _Out:
    """Terminal writer with optional colour and a typewriter beat.

    The pauses are theatre and they are small; the point is that a reader can
    watch the unwind happen in order rather than find it already finished.
    """

    def __init__(self, color: bool = True, pace: float = 0.28):
        self.color = color and sys.stdout.isatty()
        self.pace = pace

    def _c(self, text: str, style: str) -> str:
        if not self.color or style not in _STYLES:
            return text
        return f"{_STYLES[style]}{text}{_RESET}"

    def say(self, text: str = "", style: str = "", beat: float = 0.0) -> None:
        print(self._c(text, style) if style else text, flush=True)
        if beat and self.pace:
            time.sleep(beat * self.pace)

    def act(self, number: str, title: str) -> None:
        self.say()
        self.say("=" * 66, "grey")
        self.say(f"  ACT {number}   {title}", "bold")
        self.say("=" * 66, "grey")
        self.say()

    def step(self, text: str, ok: bool = True) -> None:
        mark = "  ok " if ok else "  !! "
        self.say(self._c(mark, "green" if ok else "red") + text, beat=1.0)

    def undo(self, text: str) -> None:
        self.say(self._c("  <- ", "blue") + text, beat=1.0)


# -- the world -------------------------------------------------------------------
# A JSON file, not an in-memory dict, because Act III kills the process that
# makes the mess. Damage has to outlive its author or the demo proves nothing.

def _load(world: Path) -> Dict[str, Any]:
    return json.loads(world.read_text("utf-8"))


def _save(world: Path, state: Dict[str, Any]) -> None:
    world.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _fresh(world: Path) -> None:
    _save(world, {"charges": [], "servers": [], "emails_sent": []})


_WORLD_ENV = "AGENT_SAGA_DEMO_WORLD"


def _world_path() -> Path:
    return Path(os.environ[_WORLD_ENV])


# Forward actions ---------------------------------------------------------------

def charge_card(amount: int) -> Dict[str, Any]:
    world = _world_path()
    state = _load(world)
    charge = {"id": f"ch_{len(state['charges']) + 1}", "amount": amount}
    state["charges"].append(charge)
    _save(world, state)
    return charge


def launch_server(size: str) -> Dict[str, Any]:
    world = _world_path()
    state = _load(world)
    server = {"id": f"i-{len(state['servers']) + 1}", "size": size}
    state["servers"].append(server)
    _save(world, state)
    return server


# Inverses ----------------------------------------------------------------------
# Registered by name, with JSON-safe kwargs. This is the part that makes Act III
# possible: a lambda would be perfect here and completely useless to a daemon in
# another process, which has only the log to work from.

@compensator("demo.refund")
def refund(charge_id: str) -> Dict[str, Any]:
    world = _world_path()
    state = _load(world)
    state["charges"] = [c for c in state["charges"] if c["id"] != charge_id]
    _save(world, state)
    return {"refunded": charge_id}


@compensator("demo.terminate")
def terminate(server_id: str) -> Dict[str, Any]:
    world = _world_path()
    state = _load(world)
    state["servers"] = [s for s in state["servers"] if s["id"] != server_id]
    _save(world, state)
    return {"terminated": server_id}


@compensator("demo.deconfigure")
def deconfigure(server_id: str) -> Dict[str, Any]:
    """The inverse of the step that FAILS.

    Worth explaining, because it looks unnecessary: a call that raised may
    still have landed. A timed-out POST is not a POST that did not happen, so
    the engine records that step as UNKNOWN and expects an idempotent inverse
    anyway. Omitting it is legal and the engine says so out loud -- it reports
    the step ORPHANED and the rollback INCOMPLETE, which is the honest answer
    and not the one this demo wants to be demonstrating.
    """
    return {"deconfigured": server_id}


def _report(out: _Out, world: Path, expectation: str) -> bool:
    state = _load(world)
    clean = not state["charges"] and not state["servers"]
    out.say()
    out.say("  The world, afterwards:", "bold")
    out.say(f"    charges left standing : {[c['id'] for c in state['charges']] or 'none'}",
            "red" if state["charges"] else "green")
    out.say(f"    servers left running  : {[s['id'] for s in state['servers']] or 'none'}",
            "red" if state["servers"] else "green")
    out.say(f"    emails sent           : {state['emails_sent'] or 'none'}", "grey")
    out.say()
    out.say(f"  {expectation}", "yellow")
    return clean


# -- Act I: the ordinary agent ------------------------------------------------------

def _act_one(out: _Out, world: Path) -> None:
    out.act("I", "An ordinary agent. No transaction.")
    out.say("  A three-step task, written the way most agent code is written.",
            "grey", beat=1.0)
    out.say()
    _fresh(world)

    try:
        charge = charge_card(4200)
        out.step(f"charged the customer      -> {charge['id']} ($42.00)")
        server = launch_server("m6i.large")
        out.step(f"launched their server     -> {server['id']}")
        out.say()
        raise ConnectionError("provisioning API timed out")
    except ConnectionError as exc:
        out.step(f"configure the server      -> FAILED: {exc}", ok=False)

    _report(out, world,
            "The agent stopped. The charge and the server did not. Someone has"
            "\n  to find this by hand -- and they have to know it happened.")


# -- Act II: the same calls, inside a boundary ----------------------------------------

async def _act_two(out: _Out, world: Path, wal_path: Path) -> None:
    from .decorator import saga_scope
    from .context import SagaAborted
    from .wal.file_wal import FileWAL

    out.act("II", "The same three calls, inside a saga.")
    out.say("  Nothing about the tools changed. They are wrapped, not rewritten.",
            "grey", beat=1.0)
    out.say()
    _fresh(world)

    wal = FileWAL(wal_path)
    await wal.start()
    try:
        try:
            async with saga_scope(wal=wal, name="onboarding") as saga:
                charge = await saga.execute(
                    tool="stripe.charge", semantics=ActionSemantics.COMPENSABLE,
                    forward=charge_card, forward_kwargs={"amount": 4200},
                    compensate=lambda r: Compensation(
                        fn=refund, handler="demo.refund",
                        kwargs={"charge_id": r["id"]},
                        description=f"refund {r['id']}"))
                out.step(f"charged the customer      -> {charge['id']} ($42.00)")

                server = await saga.execute(
                    tool="aws.run_instances", semantics=ActionSemantics.COMPENSABLE,
                    forward=launch_server, forward_kwargs={"size": "m6i.large"},
                    compensate=lambda r: Compensation(
                        fn=terminate, handler="demo.terminate",
                        kwargs={"server_id": r["id"]},
                        description=f"terminate {r['id']}"))
                out.step(f"launched their server     -> {server['id']}")

                out.say()

                def boom() -> None:
                    raise ConnectionError("provisioning API timed out")

                await saga.execute(
                    tool="provision.configure",
                    semantics=ActionSemantics.COMPENSABLE, forward=boom,
                    # The call raised, so its outcome is UNKNOWN -- it may have
                    # half-applied. An idempotent inverse is supplied for that
                    # possibility rather than assuming it did nothing.
                    compensate=lambda _r: Compensation(
                        fn=deconfigure, handler="demo.deconfigure",
                        kwargs={"server_id": server["id"]},
                        description="undo any partial configuration"))
        except SagaAborted as aborted:
            out.step("configure the server      -> FAILED: provisioning API timed out",
                     ok=False)
            out.say()
            out.say("  The boundary takes over. Unwinding, last in first out:",
                    "bold", beat=1.0)

            # Read the engine's own report. Printing a hardcoded "clean" here
            # would be the exact defect this project keeps fixing in other
            # people's code, so the label comes from RollbackReport or not at
            # all.
            report = getattr(aborted, "report", None)
            for step in getattr(report, "compensated", ()):
                out.undo(getattr(step, "tool", str(step)))
            out.say()
            if report is None:
                out.say("  rollback report unavailable", "yellow")
            else:
                out.say(f"  rollback reported: "
                        f"{'clean' if report.clean else 'PARTIAL -- needs a human'}",
                        "green" if report.clean else "red")
    finally:
        await wal.close()

    _report(out, world,
            "Same failure. Nothing left behind -- and 'clean' is a value the\n"
            "  caller can branch on, not a hope.")


# -- Act III: the process dies mid-transaction ------------------------------------------

_CRASH_FLAG = "--demo-crash-worker"


def _crash_worker(wal_path: str) -> None:
    """Charge the card, make the intent durable, then die without cleanup.

    `os._exit()` skips finally blocks, atexit hooks and event-loop shutdown, so
    nothing in agent_saga gets a chance to roll anything back. That is the
    point: what happens next has to come from the log alone.
    """
    from .context import SagaContext
    from .wal.file_wal import FileWAL

    async def _main() -> None:
        wal = FileWAL(wal_path)
        await wal.start()
        # A short lease so the demo does not sit for twenty seconds waiting for
        # one to expire. The daemon refuses to touch a saga whose lease is
        # still being renewed -- an owner that is alive gets to finish its own
        # work -- and only an EXPIRED lease proves the owner is gone. A PID
        # would not: they are reused within minutes. Production leaves this at
        # 5 seconds.
        saga = SagaContext(wal=wal, lease_ttl=0.2)
        await saga.begin()
        await saga.execute(
            tool="stripe.charge", semantics=ActionSemantics.COMPENSABLE,
            forward=charge_card, forward_kwargs={"amount": 9900},
            compensate=lambda r: Compensation(
                fn=refund, handler="demo.refund",
                kwargs={"charge_id": r["id"]},
                description=f"refund {r['id']}"))
        await wal.barrier()          # the intent and its inverse are on disk
        os._exit(9)                  # <- the lights go out here

    asyncio.run(_main())


async def _act_three(out: _Out, world: Path, wal_path: Path) -> None:
    from .recovery import RecoveryDaemon

    out.act("III", "The process is killed mid-transaction.")
    out.say("  This is the case a try/except cannot answer: its compensations",
            "grey")
    out.say("  live in memory, and the memory is about to be gone.", "grey", beat=1.0)
    out.say()
    _fresh(world)
    if wal_path.exists():
        wal_path.unlink()

    out.say("  starting a worker process...", beat=1.0)
    result = subprocess.run(
        [sys.executable, "-m", "agent_saga.demo", _CRASH_FLAG, str(wal_path)],
        env={**os.environ, _WORLD_ENV: str(world)},
        capture_output=True, text=True)

    out.step(f"worker charged the customer -> $99.00")
    out.say(self_kill := f"  !! worker died. exit code {result.returncode} "
            f"(killed, no cleanup ran)", "red")
    del self_kill

    state = _load(world)
    out.say()
    out.say(f"  Money taken and nobody left alive to give it back: "
            f"{[c['id'] for c in state['charges']]}", "red", beat=1.0)
    out.say()
    out.say("  Waiting for the dead worker's lease to expire...", "grey")
    out.say("  (only an EXPIRED lease proves the owner is gone -- a PID would",
            "grey")
    out.say("   not, since PIDs are reused within minutes)", "grey")
    await asyncio.sleep(0.8)
    out.say()
    out.say("  Now a DIFFERENT process reads the log:", "bold", beat=1.0)
    out.say()

    daemon = RecoveryDaemon(str(wal_path))
    outcomes = await daemon.recover_all()
    for outcome in outcomes:
        out.undo(f"saga {outcome.saga_id[:12]}... -> {outcome.resolution.value}")

    _report(out, world,
            "The process that took the money never ran a line of cleanup.\n"
            "  The refund happened anyway, because the intent was on disk\n"
            "  before the effect fired.")


# -- entry point ----------------------------------------------------------------------

def run_demo(color: bool = True, pace: float = 0.28, logs: bool = False) -> int:
    """Run all three acts. Returns a process exit code."""
    import logging

    if not logs:
        # The engine narrates itself at INFO/WARNING, which is right in
        # production and noise here -- the demo is showing the same events in
        # order. `--logs` puts the engine's own trace back.
        logging.getLogger("agent_saga").setLevel(logging.CRITICAL)

    out = _Out(color=color, pace=pace)
    workdir = Path(tempfile.mkdtemp(prefix="agent-saga-demo-"))
    world = workdir / "world.json"
    os.environ[_WORLD_ENV] = str(world)
    _fresh(world)

    out.say()
    out.say("  agent-saga -- what a transaction boundary is actually for", "bold")
    out.say("  Three acts, about five seconds. Nothing here touches a network.",
            "grey")

    _act_one(out, world)
    asyncio.run(_act_two(out, world, workdir / "act2.wal"))
    asyncio.run(_act_three(out, world, workdir / "act3.wal"))

    out.say()
    out.say("=" * 66, "grey")
    out.say("  What just happened", "bold")
    out.say("=" * 66, "grey")
    out.say()
    out.say("  Act I    an agent failed and left real damage behind.")
    out.say("  Act II   the same failure, inside a boundary. World restored,")
    out.say("           and the rollback reported itself honestly.")
    out.say("  Act III  the process was killed. A different process finished")
    out.say("           the job, from the log alone.")
    out.say()
    out.say("  That third one is the whole argument. Your agent will fail at", "yellow")
    out.say("  step 4 of 5, and someday it will fail by dying.", "yellow")
    out.say()
    out.say("  Next:", "bold")
    out.say(f"    agent-saga graph --wal {workdir / 'act2.wal'}")
    out.say("        draw the rollback fork you just watched")
    out.say("    agent-saga new myagent")
    out.say("        a working app with this wired in")
    out.say()
    out.say(f"  Artifacts kept for inspection: {workdir}", "grey")
    out.say()
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == _CRASH_FLAG:
        _crash_worker(sys.argv[2])
    else:
        sys.exit(run_demo())
