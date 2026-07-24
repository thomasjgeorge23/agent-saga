"""Human-in-the-loop approvals.

Every test here is really the same question asked differently: when the
approval machinery goes wrong, does the call get through? It must not.
"""

import json
import tempfile
import threading
import time
from pathlib import Path

import pytest

from conftest import aio

from agent_saga.approvals import (
    DENIED,
    GRANTED,
    ApprovalGateway,
    ApprovalPolicy,
    ApprovalRequest,
    ConsoleNotifier,
    EscalationLevel,
    FileApprovalStore,
    WebhookNotifier,
    request_id,
    slack_payload,
)
from agent_saga.gate import GateContext, PreFlightGate, PreFlightViolation, Rule, Verdict
from agent_saga.limits import BudgetLimit, InProcessLimitStore, set_limit_store
from agent_saga.observability import set_saga_id, set_step_id
from agent_saga.semantics import ActionSemantics
from agent_saga.wal import FileWAL


@pytest.fixture
def store(tmp_path):
    return FileApprovalStore(tmp_path / "approvals")


@pytest.fixture(autouse=True)
def correlation():
    set_saga_id("saga-test")
    set_step_id("step-test")
    yield
    set_saga_id(None)
    set_step_id(None)
    # Retire any out-of-band decider before its store is torn down.
    for stop in _DECIDERS:
        stop.set()
    _DECIDERS.clear()


def a_call(tool="wire.send", **kwargs):
    return GateContext(tool=tool, semantics=ActionSemantics.IRREVERSIBLE,
                       kwargs=kwargs or {"amount": 80_000, "to": "acct_9"})


def fast(**kwargs):
    kwargs.setdefault("timeout", 1.0)
    kwargs.setdefault("poll_interval", 0.02)
    return ApprovalPolicy(**kwargs)


# Every out-of-band decider registers its stop flag here so the autouse fixture
# below can retire it at teardown. A worker that outlives its test keeps polling
# a store whose temp directory has been deleted, and the exception it raises in
# that thread surfaces as an unraisable-exception failure attributed to whatever
# test happens to be running -- which is exactly the flake this caused.
_DECIDERS: list = []


def decide_after(store, delay, **kwargs):
    """Answer out-of-band, the way a Slack click in another process would.

    The deadline must comfortably outlive the gateway's own timeout, because the
    worker starts *before* the call it answers -- with both at 5s a loaded
    machine expired the worker first. It exits the moment it decides, the moment
    its test ends, or if the store goes away underneath it.
    """
    stop = threading.Event()
    _DECIDERS.append(stop)

    def worker():
        deadline = time.time() + 30
        while time.time() < deadline and not stop.is_set():
            try:
                pending = store.pending()
                if pending:
                    store.decide(pending[0].id, **kwargs)
                    return
            except Exception:
                return          # store torn down with the test; nothing to answer
            stop.wait(0.01)

    t = threading.Thread(target=worker, daemon=True)
    time.sleep(0) if delay == 0 else None
    t.start()
    return t


# ---------------------------------------------------------------------------
# 1. The happy path, and the shape of it
# ---------------------------------------------------------------------------

@aio
async def test_an_out_of_band_decision_releases_the_saga(store):
    """The human clicks in Slack, which reaches some web process -- not this
    one. The decision lands in the store and the waiting saga observes it."""
    gw = ApprovalGateway(store=store, policy=fast(timeout=5))
    decide_after(store, 0, granted=True, approver="risk@corp", note="verified")

    assert await gw(a_call(), Rule("r", lambda c: True, Verdict.REQUIRE_APPROVAL, "why"))


@aio
async def test_a_denial_is_a_denial(store):
    gw = ApprovalGateway(store=store, policy=fast(timeout=5))
    decide_after(store, 0, granted=False, approver="risk@corp")
    assert not await gw(a_call(), Rule("r", lambda c: True, Verdict.REQUIRE_APPROVAL, ""))


@aio
async def test_the_approver_sees_the_amount(store):
    """A decision made without the amount is a rubber stamp, and a rubber stamp
    is worse than no control -- it produces a trail that looks like oversight."""
    seen = {}

    class Capture:
        def notify(self, request, targets):
            seen.update(request.context)

    gw = ApprovalGateway(store=store, notifier=Capture(), policy=fast(timeout=0.2))
    await gw(a_call(amount=80_000, to="acct_9"),
             Rule("r", lambda c: True, Verdict.REQUIRE_APPROVAL, ""))

    assert seen["amount"] == 80_000 and seen["to"] == "acct_9"
    assert seen["semantics"] == "IRREVERSIBLE"


def test_non_scalar_context_is_summarised_not_dumped():
    from agent_saga.approvals import _default_context

    ctx = a_call(amount=1, blob=object(), payload={"k": "v"})
    out = _default_context(ctx, None)
    assert out["amount"] == 1
    assert out["blob"].startswith("<") and out["payload"].startswith("<")


# ---------------------------------------------------------------------------
# 2. Fail closed -- the whole point
# ---------------------------------------------------------------------------

@aio
async def test_an_unanswered_request_expires_and_denies(store):
    """A prompt nobody answered is not consent, and the saga cannot be held
    open forever holding its lease and locks."""
    gw = ApprovalGateway(store=store, policy=fast(timeout=0.2))
    started = time.time()
    granted = await gw(a_call(), Rule("r", lambda c: True, Verdict.REQUIRE_APPROVAL, ""))
    assert not granted
    assert time.time() - started < 3


@aio
async def test_expiry_is_recorded_as_a_decision(store):
    gw = ApprovalGateway(store=store, policy=fast(timeout=0.15))
    await gw(a_call(), Rule("r", lambda c: True, Verdict.REQUIRE_APPROVAL, ""))
    assert store.pending() == [], "an expired request must leave the queue"


@aio
async def test_an_unreachable_store_denies():
    class Broken:
        distributed = True

        def create(self, request):
            raise ConnectionError("redis is down")

        def get(self, rid):
            raise ConnectionError("redis is down")

        def decide(self, *a, **k):
            raise ConnectionError("redis is down")

        def pending(self):
            return []

    gw = ApprovalGateway(store=Broken(), policy=fast())
    assert not await gw(a_call(), Rule("r", lambda c: True, Verdict.REQUIRE_APPROVAL, ""))


@aio
async def test_a_broken_notifier_never_grants(store):
    """Nobody was told, so nobody answers, so it expires and denies. A failed
    Slack integration must not authorize spending."""
    class Broken:
        def notify(self, request, targets):
            raise RuntimeError("slack is down")

    gw = ApprovalGateway(store=store, notifier=Broken(), policy=fast(timeout=0.2))
    assert not await gw(a_call(), Rule("r", lambda c: True, Verdict.REQUIRE_APPROVAL, ""))


@aio
async def test_the_gate_turns_a_denial_into_a_refusal_before_any_effect(store):
    gw = ApprovalGateway(store=store, policy=fast(timeout=0.15))
    gate = PreFlightGate(approval_provider=gw)
    with pytest.raises(PreFlightViolation) as excinfo:
        await gate.evaluate(a_call())
    assert excinfo.value.decision.verdict is Verdict.BLOCK


# ---------------------------------------------------------------------------
# 3. Idempotency -- never ask a human twice
# ---------------------------------------------------------------------------

def test_the_request_id_is_deterministic():
    assert request_id("s", "t", "wire.send", "r") == request_id("s", "t", "wire.send", "r")
    assert request_id("s", "t", "wire.send", "r") != request_id("s", "t", "wire.send", "r2")


@aio
async def test_a_retried_step_reuses_the_existing_decision(store):
    """Re-prompting would ask a human to re-decide something they already
    answered, and the second answer would authorize a second effect."""
    prompts = []

    class Counting:
        def notify(self, request, targets):
            prompts.append(request.id)

    gw = ApprovalGateway(store=store, notifier=Counting(), policy=fast(timeout=5))
    rule = Rule("r", lambda c: True, Verdict.REQUIRE_APPROVAL, "")
    decide_after(store, 0, granted=True, approver="risk@corp")
    assert await gw(a_call(), rule)

    # Same saga, same step, same tool -- the retry must not prompt again.
    assert await gw(a_call(), rule)
    assert len(prompts) == 1


@aio
async def test_the_first_decision_wins(store):
    request = ApprovalRequest(id="abc", saga_id="s", step_id="t",
                              tool="wire.send", rule="r", reason="")
    store.create(request)
    store.decide("abc", granted=True, approver="first@corp")
    store.decide("abc", granted=False, approver="second@corp")
    assert store.get("abc").status == GRANTED
    assert store.get("abc").approver == "first@corp"


def test_creating_the_same_request_twice_is_idempotent(store):
    first = ApprovalRequest(id="abc", saga_id="s", step_id="t", tool="x",
                            rule="r", reason="one")
    store.create(first)
    store.decide("abc", granted=True, approver="a@corp")
    again = store.create(ApprovalRequest(id="abc", saga_id="s", step_id="t",
                                         tool="x", rule="r", reason="two"))
    assert again.status == GRANTED, "a second create clobbered a live decision"


# ---------------------------------------------------------------------------
# 4. Escalation
# ---------------------------------------------------------------------------

@aio
async def test_escalation_reaches_the_next_level(store):
    reached = []

    class Recording:
        def notify(self, request, targets):
            reached.append(tuple(targets))

    policy = ApprovalPolicy(timeout=0.5, poll_interval=0.02, levels=(
        EscalationLevel(targets=("@oncall",)),
        EscalationLevel(targets=("@head-of-risk",), after_seconds=0.15)))
    gw = ApprovalGateway(store=store, notifier=Recording(), policy=policy)
    await gw(a_call(), Rule("r", lambda c: True, Verdict.REQUIRE_APPROVAL, ""))

    assert ("@oncall",) in reached
    assert ("@head-of-risk",) in reached


@aio
async def test_a_prompt_answered_early_does_not_escalate(store):
    reached = []

    class Recording:
        def notify(self, request, targets):
            reached.append(tuple(targets))

    policy = ApprovalPolicy(timeout=5, poll_interval=0.02, levels=(
        EscalationLevel(targets=("@oncall",)),
        EscalationLevel(targets=("@ceo",), after_seconds=3)))
    gw = ApprovalGateway(store=store, notifier=Recording(), policy=policy)
    decide_after(store, 0, granted=True, approver="oncall@corp")
    await gw(a_call(), Rule("r", lambda c: True, Verdict.REQUIRE_APPROVAL, ""))
    assert ("@ceo",) not in reached


# ---------------------------------------------------------------------------
# 5. Break-glass
# ---------------------------------------------------------------------------

@aio
async def test_break_glass_grants_but_is_recorded_distinctly(store, tmp_path):
    """A break-glass that looks like a normal approval in the log defeats the
    point of having one."""
    wal = FileWAL(tmp_path / "w.jsonl")
    await wal.start()
    gw = ApprovalGateway(store=store, wal=wal, policy=fast(timeout=5))
    decide_after(store, 0, granted=True, approver="cto@corp",
                 note="prod incident 4471", break_glass=True)

    assert await gw(a_call(), Rule("r", lambda c: True, Verdict.REQUIRE_APPROVAL, ""))
    await wal.barrier()
    records = wal.records()
    await wal.close()

    events = [r["event"] for r in records]
    assert "APPROVAL_BREAK_GLASS" in events
    flagged = next(r for r in records if r["event"] == "APPROVAL_BREAK_GLASS")
    assert flagged["requires_review"] is True
    assert flagged["approver"] == "cto@corp" and "4471" in flagged["note"]


# ---------------------------------------------------------------------------
# 6. The audit trail
# ---------------------------------------------------------------------------

@aio
async def test_the_wal_records_who_approved_what(store, tmp_path):
    """'Prove no agent moved money without a named human' has to be answerable
    by reading the log."""
    wal = FileWAL(tmp_path / "w.jsonl")
    await wal.start()
    gw = ApprovalGateway(store=store, wal=wal, policy=fast(timeout=5))
    decide_after(store, 0, granted=True, approver="risk@corp", note="phoned client")
    await gw(a_call(amount=80_000), Rule("irreversible", lambda c: True,
                                         Verdict.REQUIRE_APPROVAL, "no undo"))
    await wal.barrier()
    records = wal.records()
    await wal.close()

    requested = next(r for r in records if r["event"] == "APPROVAL_REQUESTED")
    granted = next(r for r in records if r["event"] == "APPROVAL_GRANTED")
    assert requested["context"]["amount"] == 80_000
    assert requested["rule"] == "irreversible"
    assert granted["approver"] == "risk@corp" and granted["note"] == "phoned client"
    assert granted["request_id"] == requested["request_id"]


@aio
async def test_the_approval_trail_is_tamper_evident(store, tmp_path):
    from agent_saga.integrity import verify

    wal = FileWAL(tmp_path / "w.jsonl")
    await wal.start()
    gw = ApprovalGateway(store=store, wal=wal, policy=fast(timeout=0.15))
    await gw(a_call(), Rule("r", lambda c: True, Verdict.REQUIRE_APPROVAL, ""))
    await wal.barrier()
    records = wal.records()
    await wal.close()

    assert verify(records).intact
    # Rewriting who approved must not survive.
    tampered = [dict(r) for r in records]
    tampered[-1]["approver"] = "someone-else@corp"
    assert not verify(tampered).intact


# ---------------------------------------------------------------------------
# 7. Closing the loop with spend limits
# ---------------------------------------------------------------------------

@aio
async def test_an_over_budget_call_escalates_to_a_human(store):
    """Step 2 gave overages somewhere to escalate; this is where they go."""
    set_limit_store(InProcessLimitStore())
    gw = ApprovalGateway(store=store, policy=fast(timeout=5))
    gate = PreFlightGate(rules=[], approval_provider=gw, limits=[
        BudgetLimit("daily", arg="amount", max_total=100, window=60,
                    escalate_to_human=True)])

    await gate.evaluate(GateContext("stripe.charge", ActionSemantics.COMPENSABLE,
                                    {"amount": 100}))
    decide_after(store, 0, granted=True, approver="cfo@corp")
    set_step_id("step-over-budget")
    await gate.evaluate(GateContext("stripe.charge", ActionSemantics.COMPENSABLE,
                                    {"amount": 5_000}))
    set_limit_store(InProcessLimitStore())


@aio
async def test_an_unapproved_overage_is_refused(store):
    set_limit_store(InProcessLimitStore())
    gw = ApprovalGateway(store=store, policy=fast(timeout=0.15))
    gate = PreFlightGate(rules=[], approval_provider=gw, limits=[
        BudgetLimit("daily", arg="amount", max_total=100, window=60,
                    escalate_to_human=True)])
    with pytest.raises(PreFlightViolation):
        await gate.evaluate(GateContext("stripe.charge", ActionSemantics.COMPENSABLE,
                                        {"amount": 5_000}))
    set_limit_store(InProcessLimitStore())


# ---------------------------------------------------------------------------
# 8. Store and notifier details
# ---------------------------------------------------------------------------

def test_pending_lists_only_undecided(store):
    for i in range(3):
        store.create(ApprovalRequest(id=f"r{i}", saga_id="s", step_id=f"t{i}",
                                     tool="x", rule="r", reason=""))
    store.decide("r1", granted=True, approver="a@corp")
    assert {r.id for r in store.pending()} == {"r0", "r2"}


def test_deciding_an_unknown_request_returns_none(store):
    assert store.decide("nope", granted=True, approver="a@corp") is None


def test_a_corrupt_record_is_skipped_not_crashed(store, tmp_path):
    store.create(ApprovalRequest(id="ok", saga_id="s", step_id="t", tool="x",
                                 rule="r", reason=""))
    (store.directory / "broken.json").write_text("{not json", encoding="utf-8")
    assert [r.id for r in store.pending()] == ["ok"]


def test_slack_payload_carries_the_decision_material():
    request = ApprovalRequest(id="abc123", saga_id="saga-1", step_id="step-1",
                              tool="wire.send", rule="irreversible",
                              reason="cannot be undone",
                              context={"amount": 80_000, "to": "acct_9"})
    payload = slack_payload(request, ("@oncall",))
    body = json.dumps(payload)
    assert "wire.send" in body and "80000" in body and "acct_9" in body
    assert "@oncall" in body and "abc123" in body


def test_webhook_notifier_swallows_transport_failure():
    """It logs and leaves the request pending, which ends in a deny."""
    WebhookNotifier("http://127.0.0.1:1/nope", timeout=0.05).notify(
        ApprovalRequest(id="x", saga_id="s", step_id="t", tool="x",
                        rule="r", reason=""), ())


def test_console_notifier_runs():
    ConsoleNotifier().notify(
        ApprovalRequest(id="x", saga_id="s", step_id="t", tool="wire.send",
                        rule="r", reason="why"), ())


# ---------------------------------------------------------------------------
# 9. CLI
# ---------------------------------------------------------------------------

def test_cli_lists_and_approves(capsys, tmp_path):
    from agent_saga.cli import main

    directory = tmp_path / "approvals"
    store = FileApprovalStore(directory)
    store.create(ApprovalRequest(id="abc123", saga_id="s", step_id="t",
                                 tool="wire.send", rule="r", reason="no undo",
                                 context={"amount": 80_000}))

    assert main(["approvals", "list", "--dir", str(directory)]) == 0
    out = capsys.readouterr().out
    assert "wire.send" in out and "80000" in out

    assert main(["approvals", "approve", "abc123", "--dir", str(directory),
                 "--approver", "risk@corp"]) == 0
    assert store.get("abc123").status == GRANTED
    assert store.get("abc123").approver == "risk@corp"


def test_cli_refuses_an_anonymous_approval(capsys, tmp_path):
    """An approval with no named approver is an audit trail that proves
    nothing, which is the only thing the record exists for."""
    from agent_saga.cli import main

    directory = tmp_path / "approvals"
    FileApprovalStore(directory).create(
        ApprovalRequest(id="abc", saga_id="s", step_id="t", tool="x",
                        rule="r", reason=""))
    assert main(["approvals", "approve", "abc", "--dir", str(directory)]) == 2
    assert "approver" in capsys.readouterr().out


def test_cli_denies_and_reports_empty_queue(capsys, tmp_path):
    from agent_saga.cli import main

    directory = tmp_path / "approvals"
    store = FileApprovalStore(directory)
    store.create(ApprovalRequest(id="abc", saga_id="s", step_id="t", tool="x",
                                 rule="r", reason=""))
    assert main(["approvals", "deny", "abc", "--dir", str(directory),
                 "--approver", "risk@corp", "--note", "not justified"]) == 0
    assert store.get("abc").status == DENIED
    assert store.get("abc").note == "not justified"

    assert main(["approvals", "list", "--dir", str(directory)]) == 0
    assert "no pending" in capsys.readouterr().out


def test_cli_break_glass_is_flagged(capsys, tmp_path):
    from agent_saga.cli import main

    directory = tmp_path / "approvals"
    store = FileApprovalStore(directory)
    store.create(ApprovalRequest(id="abc", saga_id="s", step_id="t", tool="x",
                                 rule="r", reason=""))
    assert main(["approvals", "approve", "abc", "--dir", str(directory),
                 "--approver", "cto@corp", "--break-glass"]) == 0
    assert "BREAK-GLASS" in capsys.readouterr().out
    assert store.get("abc").break_glass is True


def test_cli_rejects_break_glass_on_a_denial(tmp_path):
    from agent_saga.cli import main

    assert main(["approvals", "deny", "abc", "--dir", str(tmp_path),
                 "--approver", "a@corp", "--break-glass"]) == 2


def test_cli_unknown_id(capsys, tmp_path):
    from agent_saga.cli import main

    assert main(["approvals", "approve", "missing", "--dir", str(tmp_path),
                 "--approver", "a@corp"]) == 2
    assert "no such approval" in capsys.readouterr().out


# -- #29 Teams / Discord approval notifiers -----------------------------------

def _sample_request():
    from agent_saga.approvals import ApprovalRequest
    return ApprovalRequest(id="req-abc123", saga_id="onboard-acme-1", step_id="st1",
                           tool="stripe.charge", rule="high_amount", reason="charge $5000",
                           context={"amount": 5000, "customer": "acme"})


def test_teams_payload_is_adaptive_card():
    from agent_saga.approvals import teams_payload
    p = teams_payload(_sample_request(), ["@risk-team"])
    att = p["attachments"][0]
    assert att["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert att["content"]["type"] == "AdaptiveCard"
    blob = str(p)
    assert "stripe.charge" in blob and "approvals approve req-abc123" in blob
    assert "@risk-team" in blob


def test_discord_payload_is_embed():
    from agent_saga.approvals import discord_payload
    p = discord_payload(_sample_request(), ["@risk"])
    embed = p["embeds"][0]
    assert embed["title"] == "Agent approval required"
    assert any(f["name"] == "amount" for f in embed["fields"])
    assert "approvals deny req-abc123" in str(p)


def test_teams_and_discord_notifiers_post(monkeypatch):
    import urllib.request
    from agent_saga.approvals import TeamsNotifier, DiscordNotifier
    captured = {}

    class FakeResp:
        def read(self): return b""
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = req.data
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    TeamsNotifier("https://teams.example/hook").notify(_sample_request(), ["@r"])
    assert captured["url"].startswith("https://teams.example") and b"AdaptiveCard" in captured["body"]

    DiscordNotifier("https://discord.example/hook").notify(_sample_request(), ["@r"])
    assert b"embeds" in captured["body"]


def test_notifier_failure_never_raises(monkeypatch):
    # A broken webhook must be logged, not raised -- a lost message ends in deny.
    import urllib.request
    from agent_saga.approvals import TeamsNotifier

    def boom(req, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    TeamsNotifier("https://teams.example/hook").notify(_sample_request(), [])  # no raise
