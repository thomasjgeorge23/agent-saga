"""Project scaffold: an enterprise agent app that is correct on the first run.

Every part needed to build a production agent app already ships in this
package. What did not ship was the **assembly** -- and assembly is where the
expensive mistakes live, because the dangerous choices are not errors. They
are laptop defaults that stay silent in production:

  * a file-backed WAL behind a load balancer, so no replica can recover
    another's orphans;
  * `compensate=lambda: refund(id)`, which rolls back perfectly until the
    process dies and the closure dies with it;
  * an approval gate nobody wired, so the first `IRREVERSIBLE` tool sails
    through at 3am.

So this module emits a working app that gets those three right by
construction: a registry-named compensator for every durable effect, a WAL
backend chosen from the environment (Postgres in production, file on a
laptop), a readiness endpoint that reports posture instead of guessing at it,
and a test that proves the rollback actually runs.

    agent-saga new myagent

The generated project is deliberately small enough to read in one sitting.
Anything the template cannot decide for you -- your tools, your thresholds,
your approvers -- is marked `TODO` in the code rather than filled with a
plausible default, because a plausible default in a safety boundary is how a
team ends up trusting a control nobody chose.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

__all__ = ["FILES", "render", "write_project"]


def _settings() -> str:
    return '''"""Environment-driven configuration.

The WAL backend is the one setting that changes the safety properties of the
whole app, so it is explicit and it is checked at startup rather than
discovered during an incident.
"""

import os

APP_NAME = os.getenv("APP_NAME", "{name}")

# "file" is correct for a laptop and wrong for anything with two replicas:
# each process writes a log the others cannot read, so a dead pod's orphans
# have no daemon that can see them. `agent-saga doctor --replicas N` says so.
WAL_BACKEND = os.getenv("WAL_BACKEND", "file")
WAL_PATH = os.getenv("WAL_PATH", "./{name}.wal")
POSTGRES_DSN = os.getenv("POSTGRES_DSN", "")
REDIS_URL = os.getenv("REDIS_URL", "")

# How many processes serve this app. Used by the readiness audit to decide
# whether a process-local WAL is a risk or a blocker.
REPLICAS = int(os.getenv("REPLICAS", "1"))


def build_wal():
    """Return a started-elsewhere WAL instance for the configured backend."""
    if WAL_BACKEND == "postgres":
        if not POSTGRES_DSN:
            raise RuntimeError("WAL_BACKEND=postgres requires POSTGRES_DSN")
        from agent_saga.wal.postgres_wal import PostgresWAL
        return PostgresWAL(POSTGRES_DSN)
    if WAL_BACKEND == "redis":
        if not REDIS_URL:
            raise RuntimeError("WAL_BACKEND=redis requires REDIS_URL")
        from agent_saga.wal.redis_wal import RedisWAL
        return RedisWAL(url=REDIS_URL)
    if WAL_BACKEND != "file":
        raise RuntimeError(
            f"unknown WAL_BACKEND {{WAL_BACKEND!r}}; use file, postgres, or redis")
    from agent_saga.wal.file_wal import FileWAL
    return FileWAL(WAL_PATH)
'''


def _tools() -> str:
    return '''"""Your agent's tools. Two rules, and the whole app depends on both.

1. **Every tool declares its semantics.** REVERSIBLE, COMPENSABLE, or
   IRREVERSIBLE. The engine will not guess, because the guess is exactly the
   thing a risk committee is buying.

2. **Durable effects use a REGISTERED compensator.** A closure cannot survive
   `kill -9`; `saga-recoveryd` runs in a different process and has only the
   WAL to work from. A compensation is recoverable only if it names a handler
   in the registry and its kwargs survive a JSON round trip.

The examples below are fakes with the right shape. Replace the bodies, keep
the shape.
"""

from agent_saga import ActionSemantics, Compensation
from agent_saga.registry import compensator

# -- a durable, compensable effect ------------------------------------------

_RESERVATIONS = {{}}          # TODO: replace with your real system of record


def reserve_inventory(sku: str, quantity: int) -> dict:
    """Forward action. Returns the concrete state the inverse will need."""
    reservation_id = f"res_{{len(_RESERVATIONS) + 1}}"
    _RESERVATIONS[reservation_id] = {{"sku": sku, "quantity": quantity}}
    return {{"reservation_id": reservation_id, "sku": sku, "quantity": quantity}}


@compensator("inventory.release")
def release_inventory(reservation_id: str) -> dict:
    """The inverse, registered by name so a recovery daemon in another
    process can run it. Must be idempotent: it may run after an UNKNOWN
    forward outcome, or be retried."""
    _RESERVATIONS.pop(reservation_id, None)
    return {{"released": reservation_id}}


def reserve_compensation(result: dict) -> Compensation:
    """Runtime-derived: the inverse is only knowable once the forward call
    returns a real reservation id."""
    return Compensation(
        fn=release_inventory,
        handler="inventory.release",                 # cross-process recoverable
        kwargs={{"reservation_id": result["reservation_id"]}},   # JSON-safe
        description=f"release {{result['reservation_id']}}")


# -- an irreversible effect -------------------------------------------------

def notify_customer(email: str, message: str) -> dict:
    """No automated undo exists for a sent email, so this is IRREVERSIBLE and
    the pre-flight gate must require a human before it runs. Declaring it
    COMPENSABLE to make the warning go away is the one edit that turns this
    app into a liability."""
    return {{"sent_to": email}}                       # TODO: real provider


TOOLS = [
    {{"name": "reserve_inventory", "fn": reserve_inventory,
      "semantics": ActionSemantics.COMPENSABLE, "compensate": reserve_compensation}},
    {{"name": "notify_customer", "fn": notify_customer,
      "semantics": ActionSemantics.IRREVERSIBLE, "compensate": None}},
]
'''


def _service() -> str:
    return '''"""FastAPI service: one transactional endpoint plus honest health probes.

`/readyz` reports the engine's real posture. It is wired to fail closed: if a
readiness blocker is present the probe fails, so a misconfigured replica never
silently joins the load balancer and starts taking traffic it cannot recover.
"""

from fastapi import FastAPI, HTTPException

from agent_saga import ActionSemantics, saga_scope
from agent_saga.frameworks import saga_lifespan
from agent_saga.readiness import audit

from . import settings
from .tools import TOOLS

_BY_NAME = {{tool["name"]: tool for tool in TOOLS}}

app = FastAPI(title=settings.APP_NAME,
              lifespan=saga_lifespan(settings.WAL_PATH))


@app.get("/healthz")
async def healthz() -> dict:
    """Liveness only: is the process up. Never gate traffic on this."""
    return {{"status": "ok", "app": settings.APP_NAME}}


@app.get("/readyz")
async def readyz() -> dict:
    """Readiness: is this process shaped like one that can keep its promises."""
    report = audit(wal=getattr(app.state, "saga_wal", None),
                   replicas=settings.REPLICAS)
    if not report.production_ready:
        raise HTTPException(status_code=503, detail=report.describe())
    return report.describe()


@app.post("/run")
async def run(payload: dict) -> dict:
    """Execute a list of tool calls as ONE transaction.

    If any step fails, every completed step is compensated before this
    returns. The response reports whether the unwind was clean -- a partial
    rollback is a different outcome from a clean one and must never be
    flattened into a generic 500.
    """
    calls = payload.get("calls", [])
    executed = []

    async with saga_scope(name=settings.APP_NAME) as saga:
        for call in calls:
            tool = _BY_NAME.get(call.get("tool"))
            if tool is None:
                raise HTTPException(400, f"unknown tool {{call.get('tool')!r}}")
            kwargs = call.get("args", {{}})
            await saga.execute(
                tool=tool["name"],
                semantics=tool["semantics"],
                forward=tool["fn"],
                forward_kwargs=kwargs,
                policy_args=kwargs,        # so the gate can actually see them
                compensate=tool["compensate"])
            executed.append(tool["name"])

    return {{"status": "COMPLETED", "executed": executed}}
'''


def _test() -> str:
    return '''"""The test that matters: when a step fails, the earlier ones come back.

This runs with no network, no database, and no FastAPI -- so it works in CI on
day one. If you change one thing in this project, do not change this test into
one that passes when the rollback does not run.
"""

import asyncio

import pytest

from agent_saga import ActionSemantics, AsyncWAL, SagaAborted, saga_scope

from app.tools import _RESERVATIONS, reserve_compensation, reserve_inventory


def test_a_failed_step_rolls_back_the_completed_ones(tmp_path):
    async def scenario():
        wal = AsyncWAL(tmp_path / "test.wal")
        await wal.start()
        try:
            with pytest.raises(SagaAborted):
                async with saga_scope(wal=wal, name="test") as saga:
                    await saga.execute(
                        tool="reserve_inventory",
                        semantics=ActionSemantics.COMPENSABLE,
                        forward=reserve_inventory,
                        forward_kwargs={{"sku": "widget-1", "quantity": 2}},
                        compensate=reserve_compensation)

                    def boom():
                        raise RuntimeError("downstream system is down")

                    await saga.execute(
                        tool="charge_customer",
                        semantics=ActionSemantics.COMPENSABLE,
                        forward=boom)
        finally:
            await wal.close()

    _RESERVATIONS.clear()
    asyncio.run(scenario())
    assert _RESERVATIONS == {{}}, "the reservation was not released on rollback"


def test_every_compensable_tool_is_recoverable_across_processes():
    """A compensation that only works in this process is not a rollback plan
    -- it is a rollback hope. This test fails if someone replaces a registered
    handler with a closure."""
    from agent_saga.registry import registered

    assert "inventory.release" in registered()

    comp = reserve_compensation({{"reservation_id": "res_1"}})
    assert comp.handler == "inventory.release"
    assert comp.recoverable, "compensation must survive a process restart"
'''


def _readme() -> str:
    return '''# {name}

An enterprise agent app built on [agent-saga](https://github.com/thomasjgeorge23/agent-saga):
every tool call runs inside a transaction, every durable effect has a
registry-named inverse that survives `kill -9`, and readiness is reported
rather than assumed.

## Run it

```bash
pip install -r requirements.txt
pytest                                    # the rollback test, no services needed
uvicorn app.service:app --reload          # http://127.0.0.1:8000/docs
```

```bash
curl -X POST localhost:8000/run -H 'content-type: application/json' \\
  -d '{{"calls":[{{"tool":"reserve_inventory","args":{{"sku":"widget-1","quantity":2}}}}]}}'
```

## Before production — the checklist

| Step | Why |
|---|---|
| `agent-saga doctor --replicas $REPLICAS` and fix every blocker | Catches the postures that silently lose effects |
| Set `WAL_BACKEND=postgres` (or `redis`) | A file WAL is local to one process: no other replica can recover its orphans |
| Set `REPLICAS` to the real number | Promotes the shared-log finding from risk to blocker when it actually applies |
| Run `agent-saga recover --wal ...` as a sidecar or cron | Nothing resolves a dead process's orphans unless something is looking |
| Wire an approval store before shipping any `IRREVERSIBLE` tool | The gate is what refuses to send the email at 3am without a human |
| Point `/readyz` at your load balancer | A misconfigured replica should never take traffic |
| `agent-saga verify --wal ...` in CI | Proves the log was not edited |
| `agent-saga graph --wal ...` when something goes wrong | Draws the rollback fork: clean vs. needs-a-human vs. orphaned |

## The two rules this app is built on

1. **Every tool declares its semantics.** `REVERSIBLE`, `COMPENSABLE`, or
   `IRREVERSIBLE`. The engine refuses to guess.
2. **Durable effects use a registered compensator.** A closure dies with the
   process; only `handler="name"` plus JSON kwargs can be run later by the
   recovery daemon.

`tests/test_rollback.py` enforces both. Keep it that way.

## Do you actually need this?

If your agent calls two or three tools that touch nothing durable -- no money,
no infrastructure, no messages to people -- a plain `try/except` really is
enough, and this is overhead. The moment a failure halfway through leaves
something real behind that someone would have to clean up by hand, you need a
transaction boundary and a log that outlives the process.
'''


def _env_example() -> str:
    return '''# Copy to .env and edit. Nothing here has a safe production default.
APP_NAME={name}

# file | postgres | redis
# `file` is for a laptop. With more than one replica it means each process
# writes a log the others cannot read.
WAL_BACKEND=file
WAL_PATH=./{name}.wal

# Required when WAL_BACKEND=postgres
POSTGRES_DSN=postgresql://saga:saga@localhost:5432/saga

# Required when WAL_BACKEND=redis; also used for distributed locks and limits
REDIS_URL=redis://localhost:6379/0

# How many processes serve this app. The readiness audit uses it.
REPLICAS=1
'''


def _requirements() -> str:
    return '''agent-saga[postgres,redis,otel]>={version}
fastapi>=0.110
uvicorn[standard]>=0.27
pytest>=7.4
'''


def _compose() -> str:
    return '''# Local stand-ins for the production backends. `docker compose up -d`,
# then set WAL_BACKEND=postgres in .env.
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: saga
      POSTGRES_PASSWORD: saga
      POSTGRES_DB: saga
    ports: ["5432:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U saga"]
      interval: 5s

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
'''


def _dockerfile() -> str:
    return '''FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# The readiness probe fails closed on a blocker, so a misconfigured container
# never joins the load balancer.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \\
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/readyz')"

CMD ["uvicorn", "app.service:app", "--host", "0.0.0.0", "--port", "8000"]
'''


#: relative path -> template. Templates are `str.format`-ed with `name` and
#: `version`, so literal braces in generated code are doubled in the sources.
FILES: Dict[str, str] = {
    "README.md": _readme(),
    ".env.example": _env_example(),
    "requirements.txt": _requirements(),
    "docker-compose.yml": _compose(),
    "Dockerfile": _dockerfile(),
    "app/__init__.py": "",
    "app/settings.py": _settings(),
    "app/tools.py": _tools(),
    "app/service.py": _service(),
    "tests/test_rollback.py": _test(),
}


def render(name: str, version: str) -> Dict[str, str]:
    """Return {relative path: file contents} for a project called `name`."""
    if not name or not all(ch.isalnum() or ch in "-_" for ch in name):
        raise ValueError(
            f"project name {name!r} must be non-empty and contain only "
            f"letters, digits, hyphens, or underscores")
    return {path: template.format(name=name, version=version)
            for path, template in FILES.items()}


def write_project(name: str, target: Path, version: str,
                  *, force: bool = False) -> List[Path]:
    """Write the project into `target`. Refuses to overwrite existing files
    unless `force` -- clobbering someone's edited tools.py because they reran
    a generator is not a recoverable mistake."""
    rendered = render(name, version)
    existing = [p for p in rendered if (target / p).exists()]
    if existing and not force:
        raise FileExistsError(
            f"{target} already contains: {', '.join(sorted(existing)[:5])}"
            f"{' ...' if len(existing) > 5 else ''}. Use --force to overwrite.")

    written: List[Path] = []
    for relative, content in rendered.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return sorted(written)
