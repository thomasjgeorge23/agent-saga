"""The agent-facing SDK (agentkit): one object, self-describing.

These tests pin the three things an agent relies on: safe_tool behaves inside and
outside a boundary, the guarantees manifest is stable and honest, and status()
tells the truth -- including failing *closed* when a safety signal cannot be read.
"""

import json

import pytest
from conftest import aio

from agent_saga import AgentKit, describe_guarantees, ActionSemantics
from agent_saga.agentkit import _as_semantics, _lift_compensation
from agent_saga.context import SagaAborted
from agent_saga.killswitch import KillSwitch, set_kill_switch


@pytest.fixture(autouse=True)
def _clean_switch():
    set_kill_switch(None)
    yield
    set_kill_switch(None)


# -- safe_tool: the one-call wrapper ------------------------------------------

@aio
async def test_a_committed_transaction_keeps_its_effects():
    kit = AgentKit(name="t")
    charges = []
    safe_charge = kit.safe_tool(
        lambda amount: charges.append(amount) or {"id": f"ch{len(charges)}"},
        semantics="COMPENSABLE",
        compensate=lambda r: {"handler": "refund", "kwargs": {"id": r["id"]}})

    async with kit.transaction():
        r = await safe_charge(amount=100)
    assert r["id"] == "ch1" and charges == [100]


@aio
async def test_a_failed_transaction_compensates_lifo():
    kit = AgentKit(name="t")
    undone = []
    safe_charge = kit.safe_tool(
        lambda amount: {"id": "ch1", "amount": amount},
        semantics="COMPENSABLE",
        compensate=lambda r: {"handler": "refund",
                              "fn": lambda **k: undone.append(k["id"]),
                              "kwargs": {"id": r["id"]}})

    with pytest.raises(SagaAborted):
        async with kit.transaction():
            await safe_charge(amount=4200)
            raise ValueError("downstream mismatch")
    assert undone == ["ch1"]                      # the inverse ran


@aio
async def test_a_tool_runs_untouched_outside_a_boundary():
    """The same wrapped tool must work in a plain script or test, no saga."""
    kit = AgentKit()
    safe_read = kit.safe_tool(lambda: {"rows": 3}, semantics="REVERSIBLE")
    assert await safe_read() == {"rows": 3}       # no boundary, just runs


@aio
async def test_the_wrapper_carries_its_semantics_for_introspection():
    kit = AgentKit()
    w = kit.safe_tool(lambda: None, semantics="IRREVERSIBLE")
    assert w.__agentkit_semantics__ == "IRREVERSIBLE"


# -- the guarantees manifest: stable and honest -------------------------------

def test_guarantees_is_json_serialisable_and_versioned():
    g = describe_guarantees()
    json.dumps(g)                                 # must be machine-readable
    assert g["manifest_version"] >= 1
    assert set(g["semantics"]) == {"REVERSIBLE", "COMPENSABLE", "IRREVERSIBLE"}
    # the honesty clause is not optional -- it is the point
    assert g["not_claimed"], "the manifest must state what it does NOT claim"
    assert any("developer's assertion" in s for s in g["not_claimed"])


def test_kit_guarantees_matches_the_module_manifest():
    assert AgentKit().guarantees() == describe_guarantees()


# -- status(): truthful, and fail-closed --------------------------------------

def test_status_is_ready_when_nothing_blocks():
    s = AgentKit(name="a").status()
    assert s["ready"] is True and s["blockers"] == []


def test_status_reports_a_global_halt_as_not_ready():
    ks = KillSwitch()
    set_kill_switch(ks)
    ks.halt(reason="pentest window", by="ops")
    s = AgentKit().status()
    assert s["ready"] is False
    assert any("HALTED" in b for b in s["blockers"])
    ks.resume()
    assert AgentKit().status()["ready"] is True


def test_status_fails_closed_when_the_switch_store_is_unreadable():
    """The dangerous failure is a confident green when we actually can't tell.
    An unreadable safety signal must make status NOT ready."""
    class BrokenSwitch(KillSwitch):
        def active(self, tool="", tags=()):
            raise RuntimeError("store unreachable")

    set_kill_switch(BrokenSwitch())
    s = AgentKit().status()
    assert s["ready"] is False
    assert any("unknown" in b for b in s["blockers"])


# -- the small adapters -------------------------------------------------------

def test_as_semantics_accepts_names_and_enums_and_rejects_junk():
    assert _as_semantics("compensable") is ActionSemantics.COMPENSABLE
    assert _as_semantics(ActionSemantics.REVERSIBLE) is ActionSemantics.REVERSIBLE
    with pytest.raises(ValueError):
        _as_semantics("BOGUS")


def test_lift_compensation_accepts_a_dict_or_a_compensation():
    from agent_saga.semantics import Compensation
    lifted = _lift_compensation({"id": "x"},
                                {"handler": "refund", "kwargs": {"id": "x"}})
    assert isinstance(lifted, Compensation) and lifted.handler == "refund"
    passed = Compensation(fn=None, handler="h", kwargs={})
    assert _lift_compensation(None, passed) is passed
    assert _lift_compensation(None, None) is None
    with pytest.raises(TypeError):
        _lift_compensation(None, 42)
