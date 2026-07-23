"""Human-in-the-loop approvals: durable, timed, escalating, and fail-closed.

`approval_provider` was a bare callback returning a bool. That is the right
shape and the wrong lifetime: a human takes minutes, and everything that can go
wrong in those minutes was the caller's problem. This module is the missing
half.

What a callback cannot do, and each of these is a way a real approval goes
wrong:

  * **Survive a crash.** A request that lived only in the waiting coroutine
    vanishes when the process dies, and the approver's eventual "yes" arrives
    for a request nobody is holding. Requests are written to a shared store and
    to the WAL *before* anyone is asked.

  * **Be answered from somewhere else.** The human clicks a button in Slack,
    which reaches some web process -- not the agent. The decision lands in the
    store; the waiting saga observes it there. No inbound connectivity to the
    agent is required, because agents run in places that have none.

  * **Time out.** A prompt nobody answers must not hold a saga open forever,
    holding its lease, its semantic locks and its tentative resources. A
    deadline is mandatory and expiry **denies**.

  * **Escalate.** One person is asleep. A chain asks the next after a delay,
    and records that it did.

  * **Not be asked twice.** A retried step must find its existing decision
    rather than re-prompt a human who already answered. The request id is
    derived from (saga, step, tool, rule), the same determinism the recovery
    tokens use.

Every path fails closed. A timeout denies, an unreachable store denies, an
ambiguous state denies. An approval control that lets a call through when its
own infrastructure is broken has inverted its purpose -- and unlike a limiter,
what it lets through is the action a human was specifically meant to see.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence, runtime_checkable

logger = logging.getLogger("agent_saga.approvals")

PENDING = "PENDING"
GRANTED = "GRANTED"
DENIED = "DENIED"
EXPIRED = "EXPIRED"


def request_id(saga_id: str, step_id: str, tool: str, rule: str) -> str:
    """Deterministic across processes, hosts and restarts.

    Deliberately not keyed on attempt count: a key that varied per retry would
    prompt a human again for a decision they already made, and the second answer
    would authorize a second effect.
    """
    material = f"{saga_id}:{step_id}:{tool}:{rule}".encode()
    return hashlib.sha256(material).hexdigest()[:32]


@dataclass
class ApprovalRequest:
    id: str
    saga_id: str
    step_id: str
    tool: str
    rule: str
    reason: str
    saga_name: str = ""
    """The saga's human-readable label (from saga_scope(name=...)), shown next to
    the UUID so an operator scanning the queue recognises the workflow."""
    context: dict = field(default_factory=dict)
    """What the approver needs to decide -- the amount, the recipient, the
    semantics. Whatever is here appears in the notification, so it must never
    carry a secret."""

    requested_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    status: str = PENDING
    level: int = 0
    approver: str = ""
    decided_at: float = 0.0
    note: str = ""
    break_glass: bool = False

    @property
    def decided(self) -> bool:
        return self.status in (GRANTED, DENIED, EXPIRED)

    @property
    def granted(self) -> bool:
        return self.status == GRANTED

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ApprovalRequest":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def summary(self) -> str:
        age = int(time.time() - self.requested_at)
        label = f"{self.saga_name} " if self.saga_name else ""
        return (f"[{self.status}] {label}{self.tool} -- {self.reason} "
                f"(rule {self.rule}, {age}s old, id {self.id[:12]})")


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------

@runtime_checkable
class ApprovalStore(Protocol):
    distributed: bool

    def create(self, request: ApprovalRequest) -> ApprovalRequest:
        """Record a pending request, or return the existing one unchanged.

        Must be idempotent on id: two processes racing the same retried step
        must not produce two prompts.
        """
        ...

    def get(self, request_id: str) -> Optional[ApprovalRequest]:
        ...

    def decide(self, request_id: str, *, granted: bool, approver: str,
               note: str = "", break_glass: bool = False) -> Optional[ApprovalRequest]:
        """Record a decision. First decision wins; a second is ignored."""
        ...

    def pending(self) -> list:
        ...


class FileApprovalStore:
    """One JSON file per request under a directory.

    Chosen so the default works with no infrastructure and so an operator can
    see the queue with `ls`. A decision is applied by writing to a temp file and
    os.replace-ing it, which is atomic on POSIX and Windows -- a partially
    written decision that read as GRANTED would be the worst possible failure
    in this module.
    """

    distributed = False

    def __init__(self, directory: str | Path = "./.agent-saga-approvals"):
        self.directory = Path(directory)

    def _path(self, request_id: str) -> Path:
        safe = "".join(c for c in request_id if c.isalnum() or c in "-_")
        return self.directory / f"{safe}.json"

    def _write(self, request: ApprovalRequest) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(request.id)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(request.to_dict(), fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def create(self, request: ApprovalRequest) -> ApprovalRequest:
        existing = self.get(request.id)
        if existing is not None:
            return existing
        self._write(request)
        return request

    def get(self, request_id: str) -> Optional[ApprovalRequest]:
        try:
            with open(self._path(request_id), encoding="utf-8") as fh:
                return ApprovalRequest.from_dict(json.load(fh))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.error("approval record %s is unreadable: %r", request_id, exc)
            return None

    def decide(self, request_id: str, *, granted: bool, approver: str,
               note: str = "", break_glass: bool = False) -> Optional[ApprovalRequest]:
        request = self.get(request_id)
        if request is None or request.decided:
            return request          # first decision wins
        request.status = GRANTED if granted else DENIED
        request.approver = approver
        request.note = note
        request.break_glass = break_glass
        request.decided_at = time.time()
        self._write(request)
        return request

    def update(self, request: ApprovalRequest) -> None:
        self._write(request)

    def list_all(self) -> list:
        """Every request on disk, decided or not, oldest first. Backs the CLI's
        `approvals list --status granted|denied|all` history view."""
        if not self.directory.exists():
            return []
        out = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                with open(path, encoding="utf-8") as fh:
                    out.append(ApprovalRequest.from_dict(json.load(fh)))
            except Exception:
                continue
        out.sort(key=lambda r: r.requested_at)
        return out

    def pending(self) -> list:
        return [r for r in self.list_all() if not r.decided]


class RedisApprovalStore:
    """Shared queue for a fleet.

    The agent waiting for a decision and the web process receiving the click are
    different machines. A file store cannot join them, and a queue that only
    works when both happen to be on one host is not a queue.

    Requires `pip install agent-saga[redis]`.
    """

    distributed = True

    _DECIDE_LUA = """
    local raw = redis.call('GET', KEYS[1])
    if not raw then return nil end
    local rec = cjson.decode(raw)
    if rec.status ~= 'PENDING' then return raw end
    rec.status = ARGV[1]
    rec.approver = ARGV[2]
    rec.note = ARGV[3]
    rec.break_glass = ARGV[4] == '1'
    rec.decided_at = tonumber(ARGV[5])
    local out = cjson.encode(rec)
    redis.call('SET', KEYS[1], out)
    return out
    """

    def __init__(self, url: str = "redis://localhost:6379/0", *,
                 client: Any = None, key_prefix: str = "agent-saga:approval:",
                 ttl_seconds: int = 7 * 86_400):
        self.url = url
        self.key_prefix = key_prefix
        self.ttl_seconds = ttl_seconds
        self._client = client
        if client is None:
            try:
                import redis  # noqa: F401
            except ImportError as exc:
                raise ImportError(
                    "RedisApprovalStore needs the 'redis' package.\n"
                    "    pip install agent-saga[redis]") from exc

    def _conn(self) -> Any:
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(self.url, decode_responses=True)
        return self._client

    def _key(self, request_id: str) -> str:
        return f"{self.key_prefix}{request_id}"

    def create(self, request: ApprovalRequest) -> ApprovalRequest:
        conn = self._conn()
        key = self._key(request.id)
        # SET NX: whichever process gets there first defines the request; the
        # other reads it back rather than overwriting a decision in flight.
        created = conn.set(key, json.dumps(request.to_dict()), nx=True,
                           ex=self.ttl_seconds)
        if created:
            conn.sadd(f"{self.key_prefix}pending", request.id)
            return request
        return self.get(request.id) or request

    def get(self, request_id: str) -> Optional[ApprovalRequest]:
        raw = self._conn().get(self._key(request_id))
        if not raw:
            return None
        try:
            return ApprovalRequest.from_dict(json.loads(raw))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def decide(self, request_id: str, *, granted: bool, approver: str,
               note: str = "", break_glass: bool = False) -> Optional[ApprovalRequest]:
        conn = self._conn()
        raw = conn.eval(self._DECIDE_LUA, 1, self._key(request_id),
                        GRANTED if granted else DENIED, approver, note,
                        "1" if break_glass else "0", str(time.time()))
        if not raw:
            return None
        conn.srem(f"{self.key_prefix}pending", request_id)
        return ApprovalRequest.from_dict(json.loads(raw))

    def update(self, request: ApprovalRequest) -> None:
        self._conn().set(self._key(request.id), json.dumps(request.to_dict()),
                         ex=self.ttl_seconds)

    def pending(self) -> list:
        conn = self._conn()
        out = []
        for request_id in conn.smembers(f"{self.key_prefix}pending") or []:
            request = self.get(request_id)
            if request is None:
                conn.srem(f"{self.key_prefix}pending", request_id)
            elif not request.decided:
                out.append(request)
        return out


# ---------------------------------------------------------------------------
# Notifiers
# ---------------------------------------------------------------------------

@runtime_checkable
class Notifier(Protocol):
    def notify(self, request: ApprovalRequest, targets: Sequence[str]) -> None:
        ...


class ConsoleNotifier:
    """Prints the request. For development and for an operator watching logs."""

    def notify(self, request: ApprovalRequest, targets: Sequence[str]) -> None:
        logger.warning(
            "APPROVAL NEEDED %s | tool=%s | %s | context=%s | approve with: "
            "agent-saga approvals approve %s --approver you@corp",
            request.id[:12], request.tool, request.reason,
            json.dumps(request.context, default=str)[:400], request.id)


class WebhookNotifier:
    """POSTs a JSON payload. Works with a Slack incoming webhook as-is.

    Uses urllib rather than a client library to keep the core dependency-free,
    and runs on a worker thread because a notification must never block the
    event loop that other sagas are running on.

    A notification failure never grants: it is logged and the request stays
    PENDING until it is answered or expires. Losing the message means nobody is
    asked, which correctly ends in a deny -- the opposite would let a broken
    Slack integration authorize spending.
    """

    def __init__(self, url: str, *, timeout: float = 5.0,
                 formatter: Optional[Callable[[ApprovalRequest, Sequence[str]], dict]] = None):
        self.url = url
        self.timeout = timeout
        self.formatter = formatter or slack_payload

    def notify(self, request: ApprovalRequest, targets: Sequence[str]) -> None:
        import urllib.error
        import urllib.request

        body = json.dumps(self.formatter(request, targets), default=str).encode()
        req = urllib.request.Request(
            self.url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            logger.error("could not notify %s about approval %s: %r",
                         self.url, request.id[:12], exc)


def slack_payload(request: ApprovalRequest, targets: Sequence[str]) -> dict:
    """A Slack Block Kit message carrying everything needed to decide.

    The context block is what makes this an approval rather than a prompt: an
    approver deciding on "allow this tool?" with no amount and no recipient is
    rubber-stamping, and a rubber stamp is worse than no control because it
    produces an audit trail that looks like oversight.
    """
    mention = " ".join(targets)
    fields = [f"*{k}*\n{v}" for k, v in list(request.context.items())[:8]]
    return {
        "text": f"Approval needed: {request.tool} - {request.reason}",
        "blocks": [
            {"type": "header",
             "text": {"type": "plain_text", "text": "Agent approval required"}},
            {"type": "section",
             "text": {"type": "mrkdwn",
                      "text": f"*{request.tool}*\n{request.reason}"}},
            *([{"type": "section", "fields": [
                {"type": "mrkdwn", "text": f} for f in fields]}] if fields else []),
            {"type": "context", "elements": [
                {"type": "mrkdwn",
                 "text": (f"saga `{request.saga_id[:12]}` | step "
                          f"`{request.step_id[:12]}` | rule `{request.rule}` | "
                          f"id `{request.id}`")}]},
            {"type": "section",
             "text": {"type": "mrkdwn",
                      "text": (f"{mention}\nApprove: "
                               f"`agent-saga approvals approve {request.id} "
                               f"--approver <you>`\nDeny: "
                               f"`agent-saga approvals deny {request.id} "
                               f"--approver <you>`")}},
        ],
    }


def _decision_lines(request: ApprovalRequest) -> tuple[str, str]:
    return (
        f"agent-saga approvals approve {request.id} --approver <you>",
        f"agent-saga approvals deny {request.id} --approver <you>",
    )


def teams_payload(request: ApprovalRequest, targets: Sequence[str]) -> dict:
    """A Microsoft Teams Adaptive Card (the Teams analogue of Slack Block Kit),
    wrapped in the message/attachments envelope Teams webhooks expect. Carries
    the same decision context Slack does: what, why, and how to answer."""
    approve, deny = _decision_lines(request)
    facts = [{"title": k, "value": str(v)} for k, v in list(request.context.items())[:8]]
    facts += [
        {"title": "saga", "value": request.saga_id},
        {"title": "step", "value": request.step_id},
        {"title": "rule", "value": request.rule},
    ]
    body = [
        {"type": "TextBlock", "size": "Large", "weight": "Bolder",
         "text": "Agent approval required"},
        {"type": "TextBlock", "wrap": True, "text": f"**{request.tool}** — {request.reason}"},
        {"type": "FactSet", "facts": facts},
    ]
    if targets:
        body.append({"type": "TextBlock", "wrap": True, "text": " ".join(targets)})
    body.append({"type": "TextBlock", "wrap": True, "isSubtle": True,
                 "text": f"Approve: `{approve}`\n\nDeny: `{deny}`"})
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": body,
            },
        }],
    }


def discord_payload(request: ApprovalRequest, targets: Sequence[str]) -> dict:
    """A Discord webhook message with a rich embed. Plain webhooks cannot carry
    interactive button *components* (those need a bot application), so the
    approve/deny commands travel in the embed, matching the Slack fallback."""
    approve, deny = _decision_lines(request)
    fields = [{"name": k, "value": str(v), "inline": True}
              for k, v in list(request.context.items())[:8]]
    fields.append({"name": "How to decide",
                   "value": f"Approve:\n`{approve}`\nDeny:\n`{deny}`", "inline": False})
    content = " ".join(targets) if targets else None
    return {
        "content": content or f"Approval needed: {request.tool}",
        "embeds": [{
            "title": "Agent approval required",
            "description": f"**{request.tool}** — {request.reason}",
            "color": 0xF5A623,   # amber: needs a human
            "fields": fields,
            "footer": {"text": (f"saga {request.saga_id[:12]} | "
                                f"step {request.step_id[:12]} | rule {request.rule} | "
                                f"id {request.id}")},
        }],
    }


class TeamsNotifier(WebhookNotifier):
    """POSTs a Teams Adaptive Card to an incoming-webhook / workflow URL."""

    def __init__(self, url: str, *, timeout: float = 5.0,
                 formatter: Optional[Callable[[ApprovalRequest, Sequence[str]], dict]] = None):
        super().__init__(url, timeout=timeout, formatter=formatter or teams_payload)


class DiscordNotifier(WebhookNotifier):
    """POSTs a Discord embed to a channel webhook URL."""

    def __init__(self, url: str, *, timeout: float = 5.0,
                 formatter: Optional[Callable[[ApprovalRequest, Sequence[str]], dict]] = None):
        super().__init__(url, timeout=timeout, formatter=formatter or discord_payload)


# ---------------------------------------------------------------------------
# Policy and gateway
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EscalationLevel:
    targets: tuple = ()
    after_seconds: float = 0.0


@dataclass(frozen=True)
class ApprovalPolicy:
    timeout: float = 300.0
    """Total seconds before the request expires and is DENIED. Mandatory: an
    unanswered prompt holding a saga open indefinitely also holds its lease, its
    semantic locks and its tentative resources."""

    poll_interval: float = 1.0
    levels: tuple = (EscalationLevel(),)
    allow_break_glass: bool = True

    def level_at(self, elapsed: float) -> int:
        level = 0
        for index, entry in enumerate(self.levels):
            if elapsed >= entry.after_seconds:
                level = index
        return level


class ApprovalGateway:
    """Drop-in `approval_provider` with a real lifecycle behind it.

        gate = PreFlightGate(approval_provider=ApprovalGateway(
            store=FileApprovalStore(),
            notifier=WebhookNotifier(os.environ["SLACK_WEBHOOK"]),
            policy=ApprovalPolicy(timeout=900, levels=(
                EscalationLevel(targets=("@oncall",)),
                EscalationLevel(targets=("@head-of-risk",), after_seconds=300))),
        ))
    """

    def __init__(
        self,
        *,
        store: Optional[Any] = None,
        notifier: Optional[Any] = None,
        policy: Optional[ApprovalPolicy] = None,
        wal: Any = None,
        context_builder: Optional[Callable[[Any, Any], dict]] = None,
    ):
        self.store = store or FileApprovalStore()
        self.notifier = notifier or ConsoleNotifier()
        self.policy = policy or ApprovalPolicy()
        self.wal = wal
        self.context_builder = context_builder or _default_context

    async def __call__(self, ctx: Any, rule: Any) -> bool:
        try:
            return await self.decide(ctx, rule)
        except Exception as exc:
            # The gate turns a False into a BLOCK. Raising here would surface as
            # an unhandled error rather than a refusal, and an approval system
            # that errors must still refuse.
            logger.error("approval failed for %r, denying: %r",
                         getattr(ctx, "tool", "?"), exc)
            self._record("APPROVAL_ERROR", {"tool": getattr(ctx, "tool", "?"),
                                            "error": repr(exc)})
            return False

    async def decide(self, ctx: Any, rule: Any) -> bool:
        from .observability import current_correlation, current_saga_name

        saga_id, step_id = current_correlation()
        saga_id = saga_id or "unknown"
        step_id = step_id or "unknown"
        rule_name = getattr(rule, "name", str(rule))

        rid = request_id(saga_id, step_id, ctx.tool, rule_name)
        request = ApprovalRequest(
            id=rid, saga_id=saga_id, step_id=step_id, tool=ctx.tool,
            rule=rule_name, reason=getattr(rule, "reason", "") or "approval required",
            saga_name=current_saga_name() or "",
            context=self.context_builder(ctx, rule),
            expires_at=time.time() + self.policy.timeout)

        stored = await _maybe_await(self.store.create(request))
        request = stored or request

        # A retry of a step already decided must not prompt again.
        if request.decided:
            logger.info("approval %s already %s by %s",
                        rid[:12], request.status, request.approver or "-")
            return request.granted

        self._record("APPROVAL_REQUESTED", {
            "request_id": rid, "tool": ctx.tool, "rule": rule_name,
            "reason": request.reason, "context": request.context,
            "expires_at": request.expires_at})

        return await self._await_decision(request)

    async def _await_decision(self, request: ApprovalRequest) -> bool:
        started = time.time()
        notified: set = set()
        deadline = request.expires_at or (started + self.policy.timeout)

        while True:
            elapsed = time.time() - started
            level = self.policy.level_at(elapsed)
            if level not in notified:
                notified.add(level)
                if level > 0:
                    logger.warning("escalating approval %s to level %d",
                                   request.id[:12], level)
                    self._record("APPROVAL_ESCALATED",
                                 {"request_id": request.id, "level": level})
                self._notify(request, level)

            if time.time() >= deadline:
                # Expiry denies. A prompt nobody answered is not consent, and
                # the saga cannot be held open waiting for one forever.
                await _maybe_await(self.store.decide(
                    request.id, granted=False, approver="",
                    note="expired without a decision"))
                logger.error("approval %s expired after %.0fs -- denying",
                             request.id[:12], elapsed)
                self._record("APPROVAL_EXPIRED",
                             {"request_id": request.id, "tool": request.tool,
                              "waited": round(elapsed, 1)})
                return False

            await asyncio.sleep(min(self.policy.poll_interval,
                                    max(0.0, deadline - time.time())))

            current = await _maybe_await(self.store.get(request.id))
            if current is None or not current.decided:
                continue

            granted = current.granted
            self._record("APPROVAL_GRANTED" if granted else "APPROVAL_DENIED", {
                "request_id": request.id, "tool": request.tool,
                "approver": current.approver, "note": current.note,
                "break_glass": current.break_glass,
                "waited": round(time.time() - started, 1)})
            if current.break_glass:
                # Never silent. A break-glass that looks like a normal approval
                # in the log defeats the point of having one.
                logger.error(
                    "BREAK-GLASS approval used on %s by %s (%s) -- this requires "
                    "post-hoc review", request.tool, current.approver, current.note)
                self._record("APPROVAL_BREAK_GLASS", {
                    "request_id": request.id, "tool": request.tool,
                    "approver": current.approver, "note": current.note,
                    "requires_review": True})
            logger.info("approval %s %s by %s", request.id[:12],
                        current.status, current.approver or "-")
            return granted

    def _notify(self, request: ApprovalRequest, level: int) -> None:
        try:
            targets = self.policy.levels[level].targets if self.policy.levels else ()
        except IndexError:
            targets = ()
        try:
            self.notifier.notify(request, targets)
        except Exception as exc:
            # Nobody was told, so nobody answers, so it expires and denies.
            # Correct, and loud.
            logger.error("notifier failed for approval %s: %r", request.id[:12], exc)

    def _record(self, event: str, payload: dict) -> None:
        if self.wal is None:
            return
        try:
            self.wal.append(event, payload)
        except Exception as exc:
            logger.error("could not record %s in the WAL: %r", event, exc)


def _default_context(ctx: Any, rule: Any) -> dict:
    """What the approver sees. Arguments are included because a decision made
    without the amount is a rubber stamp."""
    out: dict = {"semantics": getattr(getattr(ctx, "semantics", None), "value", "?")}
    for key, value in (getattr(ctx, "kwargs", None) or {}).items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        else:
            out[key] = f"<{type(value).__name__}>"
    return out


async def _maybe_await(value: Any) -> Any:
    if asyncio.isfuture(value) or asyncio.iscoroutine(value):
        return await value
    return value


# Columns persisted for each approval, in a stable order for INSERT/SELECT.
_PG_COLUMNS = (
    "id", "saga_id", "step_id", "tool", "rule", "reason", "saga_name",
    "context", "requested_at", "expires_at", "status", "level", "approver",
    "decided_at", "note", "break_glass",
)


class PostgresApprovalStore:
    """Relational approval store on PostgreSQL, for a fleet that already runs
    Postgres and would rather not add Redis.

    Connections are the scarce resource. Pass a *pool* via ``from_pool`` and each
    operation borrows a connection and returns it immediately, so a busy risk
    queue cannot exhaust the database's connection slots. A single injected
    ``connection`` is also supported (simplest setup), and with neither the store
    degrades to an in-memory map so tests and local runs work with no database.

    The connection is used through the DB-API 2.0 surface (``cursor()``,
    ``execute``, ``fetchone``/``fetchall``, ``commit``) that psycopg2/psycopg
    expose, so any DB-API-compatible pool or connection works.
    """

    distributed = True

    def __init__(self, table_name: str = "saga_approvals", connection: Any = None,
                 *, pool: Any = None):
        if not table_name.isidentifier():
            raise ValueError(f"invalid table_name identifier: {table_name!r}")
        self.table_name = table_name
        self.connection = connection
        self._pool = pool
        self._memory_backup: dict[str, ApprovalRequest] = {}
        if connection is not None or pool is not None:
            self._ensure_schema()

    @classmethod
    def from_pool(cls, pool: Any, table_name: str = "saga_approvals") -> "PostgresApprovalStore":
        """Build a store backed by a DB-API connection pool (e.g. psycopg2's
        ThreadedConnectionPool, or any object with getconn()/putconn() or a
        connection() context manager). Connections are borrowed per operation."""
        return cls(table_name=table_name, pool=pool)

    # -- connection handling ----------------------------------------------

    class _Borrowed:
        """Context manager yielding a live connection, returned to the pool (or
        left intact for a single injected connection) when the block exits --
        even on error, so a failed query never leaks a connection."""

        def __init__(self, store: "PostgresApprovalStore"):
            self.store = store
            self._conn = None
            self._from_pool = False
            self._cm = None

        def __enter__(self):
            pool = self.store._pool
            if pool is not None:
                if hasattr(pool, "getconn"):            # psycopg2 pool
                    self._conn = pool.getconn()
                    self._from_pool = True
                elif hasattr(pool, "connection"):       # context-manager pool
                    self._cm = pool.connection()
                    self._conn = self._cm.__enter__()
                else:
                    raise TypeError(
                        "pool must expose getconn()/putconn() or connection()")
            else:
                self._conn = self.store.connection      # may be None -> memory
            return self._conn

        def __exit__(self, exc_type, exc, tb):
            pool = self.store._pool
            if self._cm is not None:
                self._cm.__exit__(exc_type, exc, tb)
            elif self._from_pool and pool is not None and hasattr(pool, "putconn"):
                pool.putconn(self._conn)
            return False

    def _borrow(self):
        return self._Borrowed(self)

    def _uses_db(self) -> bool:
        return self.connection is not None or self._pool is not None

    def _ensure_schema(self) -> None:
        ddl = (
            f"CREATE TABLE IF NOT EXISTS {self.table_name} ("
            "id TEXT PRIMARY KEY, saga_id TEXT, step_id TEXT, tool TEXT, "
            "rule TEXT, reason TEXT, saga_name TEXT, context JSONB, "
            "requested_at DOUBLE PRECISION, expires_at DOUBLE PRECISION, "
            "status TEXT, level INTEGER, approver TEXT, "
            "decided_at DOUBLE PRECISION, note TEXT, break_glass BOOLEAN)"
        )
        try:
            with self._borrow() as conn:
                if conn is None:
                    return
                self._execute(conn, ddl, ())
                self._commit(conn)
        except Exception as exc:
            logger.warning("could not ensure approvals schema: %r", exc)

    # -- row <-> request mapping ------------------------------------------

    @staticmethod
    def _to_row(req: ApprovalRequest) -> tuple:
        return (
            req.id, req.saga_id, req.step_id, req.tool, req.rule, req.reason,
            req.saga_name, json.dumps(req.context or {}), req.requested_at,
            req.expires_at, req.status, req.level, req.approver, req.decided_at,
            req.note, req.break_glass,
        )

    @staticmethod
    def _from_row(row: Sequence) -> ApprovalRequest:
        d = dict(zip(_PG_COLUMNS, row))
        ctx = d.get("context")
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except Exception:
                ctx = {}
        d["context"] = ctx or {}
        return ApprovalRequest(**d)

    @staticmethod
    def _execute(conn: Any, sql: str, params: tuple):
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            try:
                return cur.fetchall()
            except Exception:
                return None
        finally:
            cur.close()

    @staticmethod
    def _commit(conn: Any) -> None:
        commit = getattr(conn, "commit", None)
        if callable(commit):
            commit()

    # -- store interface ---------------------------------------------------

    def create(self, request: ApprovalRequest) -> ApprovalRequest:
        existing = self.get(request.id)
        if existing is not None:
            return existing
        if not self._uses_db():
            self._memory_backup[request.id] = request
            return request
        cols = ", ".join(_PG_COLUMNS)
        placeholders = ", ".join(["%s"] * len(_PG_COLUMNS))
        sql = (f"INSERT INTO {self.table_name} ({cols}) VALUES ({placeholders}) "
               f"ON CONFLICT (id) DO NOTHING")
        with self._borrow() as conn:
            self._execute(conn, sql, self._to_row(request))
            self._commit(conn)
        return self.get(request.id) or request

    def get(self, request_id: str) -> Optional[ApprovalRequest]:
        if not self._uses_db():
            return self._memory_backup.get(request_id)
        sql = f"SELECT {', '.join(_PG_COLUMNS)} FROM {self.table_name} WHERE id = %s"
        with self._borrow() as conn:
            rows = self._execute(conn, sql, (request_id,))
        return self._from_row(rows[0]) if rows else None

    def decide(self, request_id: str, *, granted: bool, approver: str,
               note: str = "", break_glass: bool = False) -> Optional[ApprovalRequest]:
        req = self.get(request_id)
        if req is None or req.decided:
            return req
        req.status = GRANTED if granted else DENIED
        req.approver = approver
        req.note = note
        req.break_glass = break_glass
        req.decided_at = time.time()
        if not self._uses_db():
            self._memory_backup[request_id] = req
            return req
        # First-decision-wins: only update a row still PENDING.
        sql = (f"UPDATE {self.table_name} SET status=%s, approver=%s, note=%s, "
               f"break_glass=%s, decided_at=%s WHERE id=%s AND status=%s")
        with self._borrow() as conn:
            self._execute(conn, sql, (req.status, approver, note, break_glass,
                                      req.decided_at, request_id, PENDING))
            self._commit(conn)
        return self.get(request_id) or req

    def pending(self) -> list:
        if not self._uses_db():
            return [r for r in self._memory_backup.values() if not r.decided]
        sql = (f"SELECT {', '.join(_PG_COLUMNS)} FROM {self.table_name} "
               f"WHERE status = %s ORDER BY requested_at")
        with self._borrow() as conn:
            rows = self._execute(conn, sql, (PENDING,)) or []
        return [self._from_row(r) for r in rows]


_APPROVAL_STORE: Any = None


def get_approval_store(store_path: str = "./approvals.json") -> ApprovalStore:
    global _APPROVAL_STORE
    if _APPROVAL_STORE is None:
        _APPROVAL_STORE = FileApprovalStore(store_path)
    return _APPROVAL_STORE


def set_approval_store(store: ApprovalStore) -> None:
    global _APPROVAL_STORE
    _APPROVAL_STORE = store


__all__ = [
    "ApprovalRequest", "ApprovalStore", "FileApprovalStore", "RedisApprovalStore", "PostgresApprovalStore",
    "ApprovalGateway", "ApprovalPolicy", "EscalationLevel",
    "Notifier", "ConsoleNotifier", "WebhookNotifier", "slack_payload",
    "TeamsNotifier", "DiscordNotifier", "teams_payload", "discord_payload",
    "request_id", "PENDING", "GRANTED", "DENIED", "EXPIRED",
    "get_approval_store", "set_approval_store",
]

