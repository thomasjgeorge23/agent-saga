"""Saga-aware MCP proxy.

The claim is that an unmodified agent gets transactional boundaries, a policy
gate, spend limits and an audit trail. So the tests are written from the
agent's side: it calls tools, something goes wrong, and the world has to end up
in the right state without the agent having cooperated.
"""

import json
import tempfile
from pathlib import Path

import pytest

from conftest import aio

from agent_saga.gate import PreFlightGate, PreFlightViolation, Rule, Verdict
from agent_saga.limits import (
    BudgetLimit,
    InProcessLimitStore,
    set_limit_store,
)
from agent_saga.mcp import (
    PolicyError,
    ProxyPolicy,
    SagaMCPProxy,
    extract,
    load_policy,
    load_policy_file,
)
from agent_saga.semantics import ActionSemantics
from agent_saga.wal import FileWAL

POLICY = {
    "mode": "enforce",
    "tools": {
        "stripe__create_charge": {
            "semantics": "COMPENSABLE",
            "compensate": {"tool": "stripe__create_refund", "args": {"charge": "$.id"}},
            "policy_args": {"amount": "$.amount"},
        },
        "crm__update": {
            "semantics": "COMPENSABLE",
            "compensate": {"tool": "crm__update",
                           "from_arguments": {"record_id": "$.record_id"},
                           "static": {"status": "lead"}},
        },
        "search_docs": {"semantics": "REVERSIBLE"},
        "send_email": {"semantics": "IRREVERSIBLE"},
    },
}


class FakeUpstream:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    async def __call__(self, tool, arguments):
        self.calls.append((tool, dict(arguments)))
        if tool == self.fail_on:
            raise RuntimeError(f"{tool} exploded")
        if tool == "stripe__create_charge":
            return {"id": "ch_123", "amount": arguments.get("amount")}
        return {"ok": True}

    @property
    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture(autouse=True)
def fresh_limits():
    set_limit_store(InProcessLimitStore())
    yield
    set_limit_store(InProcessLimitStore())


async def make_proxy(upstream, policy=None, **kwargs):
    wal = FileWAL()
    await wal.start()
    proxy = SagaMCPProxy(load_policy(policy or POLICY), upstream, wal=wal, **kwargs)
    return proxy, wal


# ---------------------------------------------------------------------------
# 1. The headline: rollback through a proxy, agent unaware
# ---------------------------------------------------------------------------

@aio
async def test_an_unmodified_agents_calls_are_unwound():
    up = FakeUpstream()
    proxy, wal = await make_proxy(up)

    await proxy.call("search_docs", {"q": "refunds"})
    await proxy.call("stripe__create_charge", {"amount": 4200, "customer": "cus_1"})
    await proxy.call("crm__update", {"record_id": "acct_1", "status": "customer"})
    report = await proxy.rollback("the model went off the rails")
    await wal.close()

    assert report["clean"], report["summary"]
    assert report["rolled_back"] == 2
    # LIFO, and the refund carries an id that only existed at runtime.
    assert up.names[3:] == ["crm__update", "stripe__create_refund"]
    assert up.calls[-1][1] == {"charge": "ch_123"}
    assert up.calls[3][1]["status"] == "lead"


@aio
async def test_a_read_is_gated_but_not_put_on_the_rollback_stack():
    """Listing searches as UNRESOLVED in every rollback report trains an
    operator to ignore the report."""
    up = FakeUpstream()
    proxy, wal = await make_proxy(up)
    await proxy.call("search_docs", {"q": "x"})
    report = await proxy.rollback()
    await wal.close()
    assert report["rolled_back"] == 0 and report["clean"]


@aio
async def test_a_failing_tool_is_still_compensated_because_it_may_have_landed():
    """A tool call that raised is UNKNOWN, not "didn't happen" -- an MCP server
    that times out may well have done the work. So its inverse runs too."""
    up = FakeUpstream(fail_on="send_nowhere")
    proxy, wal = await make_proxy(up)

    await proxy.call("stripe__create_charge", {"amount": 4200})
    report = await proxy.rollback("step failed")
    await wal.close()

    assert report["clean"]
    assert "stripe__create_refund" in up.names


@aio
async def test_a_failed_compensation_halts_instead_of_unwinding_past_it():
    """If undoing step N fails, step N-1's inverse may be operating on state
    that is no longer what it assumed. Halting leaves a loud, resolvable
    report; continuing turns a partial rollback into a worse outcome."""
    up = FakeUpstream(fail_on="crm__update")
    proxy, wal = await make_proxy(up)

    await proxy.call("stripe__create_charge", {"amount": 4200})
    with pytest.raises(RuntimeError):
        await proxy.call("crm__update", {"record_id": "acct_1"})
    report = await proxy.rollback("step failed")
    await wal.close()

    assert not report["clean"]
    assert report["failed"] == ["crm__update"]
    # The charge is not silently dropped -- it is reported for a human.
    assert "UNRESOLVED" in report["summary"]
    assert "stripe__create_refund" not in up.names


# ---------------------------------------------------------------------------
# 2. Undeclared tools
# ---------------------------------------------------------------------------

@aio
async def test_an_undeclared_tool_never_reaches_the_server():
    up = FakeUpstream()
    proxy, wal = await make_proxy(up)
    with pytest.raises(PreFlightViolation) as excinfo:
        await proxy.call("wire__send", {"amount": 90_000})
    await wal.close()

    assert up.calls == [], "an unclassified tool reached a real system"
    assert "not declared" in excinfo.value.decision.reason
    assert proxy.blocked == 1


@aio
async def test_a_deployment_can_declare_its_unknowns_reversible():
    """An explicit claim in a signed file, never a default."""
    up = FakeUpstream()
    policy = {**POLICY, "unknown_semantics": "REVERSIBLE"}
    proxy, wal = await make_proxy(up, policy)
    await proxy.call("some__unlisted_read", {"q": "x"})
    await wal.close()
    assert up.names == ["some__unlisted_read"]


@aio
async def test_observe_mode_forwards_everything_and_learns_the_surface():
    up = FakeUpstream()
    wal = FileWAL()
    await wal.start()
    proxy = SagaMCPProxy(ProxyPolicy(mode="observe"), up, wal=wal)

    await proxy.call("wire__send", {"amount": 1, "to": "acct"})
    await proxy.call("wire__send", {"amount": 2, "to": "acct"})
    await proxy.call("search_docs", {"q": "x"})
    await wal.close()

    assert len(up.calls) == 3
    skeleton = proxy.policy_skeleton()
    assert set(skeleton["tools"]) == {"wire__send", "search_docs"}
    # Everything comes back as IRREVERSIBLE: a generator that guessed
    # COMPENSABLE would be asserting a real effect is undoable, from a name.
    assert all(t["semantics"] == "IRREVERSIBLE" for t in skeleton["tools"].values())
    assert "amount" in skeleton["tools"]["wire__send"]["description"]
    assert skeleton["mode"] == "enforce"


# ---------------------------------------------------------------------------
# 3. The gate and limits apply through the proxy
# ---------------------------------------------------------------------------

@aio
async def test_irreversible_tools_still_need_a_human():
    up = FakeUpstream()
    proxy, wal = await make_proxy(up)          # default gate, no approver
    with pytest.raises(PreFlightViolation):
        await proxy.call("send_email", {"to": "everyone@corp.com"})
    await wal.close()
    assert up.calls == []


@aio
async def test_spend_limits_apply_to_a_proxied_agent():
    """The budget has to read an amount out of an arbitrary MCP schema, which
    is what policy_args exists for."""
    up = FakeUpstream()
    gate = PreFlightGate(rules=[], limits=[
        BudgetLimit("daily", arg="amount", max_total=10_000, window=3_600)])
    proxy, wal = await make_proxy(up, gate=gate)

    for _ in range(2):
        await proxy.call("stripe__create_charge", {"amount": 4200})
    with pytest.raises(PreFlightViolation) as excinfo:
        await proxy.call("stripe__create_charge", {"amount": 4200})
    await wal.close()

    assert "daily" in excinfo.value.decision.rule
    assert up.names.count("stripe__create_charge") == 2


@aio
async def test_a_blocked_call_is_not_recorded_as_an_effect():
    up = FakeUpstream()
    gate = PreFlightGate(rules=[
        Rule("no-charges", lambda c: c.tool == "stripe__create_charge",
             Verdict.BLOCK, "not allowed")])
    proxy, wal = await make_proxy(up, gate=gate)

    with pytest.raises(PreFlightViolation):
        await proxy.call("stripe__create_charge", {"amount": 1})
    report = await proxy.rollback()
    await wal.close()
    assert report["rolled_back"] == 0 and up.calls == []


# ---------------------------------------------------------------------------
# 4. Boundaries
# ---------------------------------------------------------------------------

@aio
async def test_a_clean_disconnect_commits_a_session():
    up = FakeUpstream()
    proxy, wal = await make_proxy(up)
    await proxy.call("stripe__create_charge", {"amount": 4200})
    await proxy.close(failed=False)
    await wal.close()
    assert "stripe__create_refund" not in up.names


@aio
async def test_a_failed_disconnect_rolls_a_session_back():
    up = FakeUpstream()
    proxy, wal = await make_proxy(up)
    await proxy.call("stripe__create_charge", {"amount": 4200})
    await proxy.close(failed=True)
    await wal.close()
    assert "stripe__create_refund" in up.names


@aio
async def test_explicit_boundary_rolls_back_a_saga_nobody_committed():
    """The caller opted into declaring boundaries and did not declare this one.
    Committing on a boundary nobody claimed makes a half-finished run permanent."""
    up = FakeUpstream()
    proxy, wal = await make_proxy(up, boundary="explicit")
    await proxy.call("stripe__create_charge", {"amount": 4200})
    await proxy.close(failed=False)
    await wal.close()
    assert "stripe__create_refund" in up.names


@aio
async def test_explicit_boundary_exposes_control_tools_to_the_model():
    up = FakeUpstream()
    proxy, wal = await make_proxy(up, boundary="explicit")
    tools = proxy.decorate_tools([{"name": "search_docs"}])
    names = [t["name"] for t in tools]
    assert names[0] == "search_docs", "upstream tools must pass through unchanged"
    assert {"saga_commit", "saga_rollback", "saga_status"} <= set(names)

    await proxy.call("stripe__create_charge", {"amount": 4200})
    status = await proxy.call("saga_status", {})
    assert status["open"] and status["steps"][0]["undoable"]
    result = await proxy.call("saga_rollback", {"reason": "model changed its mind"})
    await wal.close()
    assert result["clean"] and "stripe__create_refund" in up.names


@aio
async def test_session_boundary_does_not_show_control_tools():
    """A model that sees a different tool list behaves differently."""
    up = FakeUpstream()
    proxy, wal = await make_proxy(up)
    tools = proxy.decorate_tools([{"name": "search_docs"}])
    await wal.close()
    assert [t["name"] for t in tools] == ["search_docs"]


@aio
async def test_boundary_none_gates_without_stacking():
    up = FakeUpstream()
    proxy, wal = await make_proxy(up, boundary="none")
    await proxy.call("stripe__create_charge", {"amount": 4200})
    report = await proxy.rollback()
    await wal.close()
    assert report["rolled_back"] == 0
    assert up.names == ["stripe__create_charge"]


# ---------------------------------------------------------------------------
# 5. Policy validation -- the checks a live proxy cannot make
# ---------------------------------------------------------------------------

def test_compensable_without_an_inverse_is_rejected():
    """It would report a clean rollback while the charge stands."""
    with pytest.raises(PolicyError) as excinfo:
        load_policy({"tools": {"t": {"semantics": "COMPENSABLE"}}})
    assert "clean rollback" in str(excinfo.value)


def test_irreversible_with_an_inverse_is_rejected():
    with pytest.raises(PolicyError):
        load_policy({"tools": {"t": {"semantics": "IRREVERSIBLE",
                                     "compensate": {"tool": "u"}}}})


def test_compensation_without_a_tool_is_rejected():
    with pytest.raises(PolicyError):
        load_policy({"tools": {"t": {"semantics": "COMPENSABLE",
                                     "compensate": {"args": {}}}}})


@pytest.mark.parametrize("bad", [
    {"tools": {"t": {"semantics": "MAYBE"}}},
    {"mode": "whatever"},
    {"tools": {"t": 7}},
])
def test_malformed_policies_are_rejected(bad):
    with pytest.raises(PolicyError):
        load_policy(bad)


def test_a_bare_semantics_string_is_accepted():
    policy = load_policy({"tools": {"search": "REVERSIBLE"}})
    assert policy.get("search").semantics is ActionSemantics.REVERSIBLE


def test_policy_file_round_trip():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "p.json"
        path.write_text(json.dumps(POLICY), encoding="utf-8")
        policy = load_policy_file(str(path))
    assert policy.get("stripe__create_charge").compensate.tool == "stripe__create_refund"


def test_invalid_json_names_the_file():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "p.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(PolicyError) as excinfo:
            load_policy_file(str(path))
    assert "p.json" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 6. Path extraction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("$.id", "ch_1"),
    ("$.nested.charge.id", "deep"),
    ("$.items[0].sku", "A"),
    ("$.items[1].sku", "B"),
    ("$.missing", None),
    ("$.nested.missing.deeper", None),
    ("$.items[9].sku", None),
    ("literal", "literal"),
])
def test_extract(path, expected):
    source = {"id": "ch_1", "nested": {"charge": {"id": "deep"}},
              "items": [{"sku": "A"}, {"sku": "B"}]}
    assert extract(source, path) == expected


@aio
async def test_a_compensation_that_cannot_be_addressed_is_orphaned_loudly():
    """The forward result did not carry the id the policy points at. Sending a
    refund with a null charge id would fail at 3am against a real API instead."""
    class NoId:
        def __init__(self):
            self.calls = []

        async def __call__(self, tool, arguments):
            self.calls.append((tool, dict(arguments)))
            return {"unexpected": "shape"}

    up = NoId()
    proxy, wal = await make_proxy(up)
    await proxy.call("stripe__create_charge", {"amount": 4200})
    report = await proxy.rollback()
    await wal.close()

    assert not report["clean"]
    assert report["orphaned"] == ["stripe__create_charge"]
    assert [c[0] for c in up.calls] == ["stripe__create_charge"]


# ---------------------------------------------------------------------------
# 7. Recovery across the process boundary
# ---------------------------------------------------------------------------

@aio
async def test_compensations_are_recorded_by_name_for_the_daemon():
    """A closure over a live connection cannot cross a process boundary, so the
    WAL has to carry a registry handler name and JSON kwargs."""
    up = FakeUpstream()
    with tempfile.TemporaryDirectory() as d:
        wal = FileWAL(Path(d) / "w.jsonl")
        await wal.start()
        proxy = SagaMCPProxy(load_policy(POLICY), up, wal=wal,
                             server_name="stripe-mcp")
        await proxy.call("stripe__create_charge", {"amount": 4200})
        await wal.barrier()
        records = wal.records()
        await wal.close()

    committed = [r for r in records if r.get("event") == "STEP_COMMITTED"]
    comp = committed[-1]["compensation"]
    assert comp["handler"] == "mcp.tool_call"
    assert comp["kwargs"]["tool"] == "stripe__create_refund"
    assert comp["kwargs"]["arguments"] == {"charge": "ch_123"}
    assert comp["kwargs"]["server"] == "stripe-mcp"
    assert json.dumps(comp["kwargs"]), "kwargs must survive a JSON round trip"


@aio
async def test_recovery_without_a_dispatcher_escalates_rather_than_guessing():
    from agent_saga.mcp.proxy import _compensate_via_mcp, set_mcp_dispatcher

    set_mcp_dispatcher(None)
    with pytest.raises(RuntimeError) as excinfo:
        await _compensate_via_mcp(server="s", tool="t", arguments={})
    assert "dispatcher" in str(excinfo.value)


@aio
async def test_an_installed_dispatcher_is_used():
    from agent_saga.mcp.proxy import _compensate_via_mcp, set_mcp_dispatcher

    seen = []

    async def dispatch(server, tool, arguments):
        seen.append((server, tool, arguments))
        return {"ok": True}

    set_mcp_dispatcher(dispatch)
    try:
        await _compensate_via_mcp(server="s", tool="stripe__create_refund",
                                  arguments={"charge": "ch_1"})
    finally:
        set_mcp_dispatcher(None)
    assert seen == [("s", "stripe__create_refund", {"charge": "ch_1"})]


# ---------------------------------------------------------------------------
# 8. The audit trail
# ---------------------------------------------------------------------------

@aio
async def test_proxied_calls_produce_a_verifiable_chain():
    from agent_saga.integrity import verify

    up = FakeUpstream()
    with tempfile.TemporaryDirectory() as d:
        wal = FileWAL(Path(d) / "w.jsonl")
        await wal.start()
        proxy = SagaMCPProxy(load_policy(POLICY), up, wal=wal)
        await proxy.call("stripe__create_charge", {"amount": 4200})
        await proxy.rollback("done")
        await wal.barrier()
        records = wal.records()
        await wal.close()

    assert verify(records).intact
    assert any(r.get("event") == "COMPENSATED" for r in records)


# -- #34 observe-mode policy auto-generator -----------------------------------

def _obs(now):
    return {
        "db.get_user":     {"calls": 50, "arg_keys": ["user_id"], "first_seen": now-10, "last_seen": now},
        "stripe.charge":   {"calls": 8,  "arg_keys": ["amount", "customer"], "first_seen": now-20, "last_seen": now},
        "email.send":      {"calls": 3,  "arg_keys": ["to", "body"], "first_seen": now-5, "last_seen": now},
        "crm.create_lead": {"calls": 12, "arg_keys": ["name", "email"], "first_seen": now-30, "last_seen": now},
    }


def test_skeleton_infers_semantics_suggestions():
    import time
    from agent_saga.mcp.policy import skeleton_from_observations
    sk = skeleton_from_observations(_obs(time.time()))["tools"]
    assert sk["db.get_user"]["_suggested_semantics"] == "REVERSIBLE"
    assert sk["stripe.charge"]["_suggested_semantics"] == "COMPENSABLE"
    assert sk["email.send"]["_suggested_semantics"] == "IRREVERSIBLE"
    assert sk["crm.create_lead"]["_suggested_semantics"] == "COMPENSABLE"


def test_skeleton_active_semantics_stays_irreversible():
    import time
    from agent_saga.mcp.policy import skeleton_from_observations
    sk = skeleton_from_observations(_obs(time.time()))["tools"]
    # the generator never auto-upgrades; every active semantics is the safe default
    assert all(e["semantics"] == "IRREVERSIBLE" for e in sk.values())


def test_skeleton_emits_compensation_stub_for_write_tools():
    import time
    from agent_saga.mcp.policy import skeleton_from_observations
    sk = skeleton_from_observations(_obs(time.time()))["tools"]
    stub = sk["crm.create_lead"]["_compensate_stub"]
    assert stub["tool"] == "create_lead_undo"
    assert stub["from_arguments"] == {"email": "email", "name": "name"}
    # read-only tools get no stub
    assert "_compensate_stub" not in sk["db.get_user"]


def test_skeleton_recommends_rate_limit_from_frequency():
    import time
    from agent_saga.mcp.policy import skeleton_from_observations
    sk = skeleton_from_observations(_obs(time.time()))["tools"]
    # 50 calls over 10s -> ~5/s -> *60*2 headroom = 600/60s
    assert sk["db.get_user"]["_rate_limit"]["max_calls"] == 600
    assert sk["db.get_user"]["_rate_limit"]["window_seconds"] == 60.0


def test_enriched_skeleton_still_loads():
    import time
    from agent_saga.mcp.policy import skeleton_from_observations, load_policy
    sk = skeleton_from_observations(_obs(time.time()))
    policy = load_policy(sk)               # advisory _keys ignored, no error
    assert policy.mode == "enforce" and len(policy.tools) == 4


def test_render_policy_yaml():
    import time
    from agent_saga.mcp.policy import skeleton_from_observations, render_policy_yaml
    text = render_policy_yaml(skeleton_from_observations(_obs(time.time())))
    assert "mode:" in text and "tools:" in text and "_suggested_semantics" in text
