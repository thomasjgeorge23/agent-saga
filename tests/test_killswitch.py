"""Kill switch and quarantine.

The question every test asks: when an operator says stop, does it stop -- and
when the switch's own infrastructure fails, does the system do the least bad
thing?
"""

import json
import time

import pytest

from conftest import aio

from agent_saga.gate import GateContext, PreFlightGate, PreFlightViolation
from agent_saga.killswitch import (
    DRAINING,
    HALTED,
    FileSwitchStore,
    Halted,
    KillSwitch,
    Switch,
    get_kill_switch,
    set_kill_switch,
)
from agent_saga.limits import BudgetLimit, InProcessLimitStore, set_limit_store
from agent_saga.semantics import ActionSemantics
from agent_saga.wal import FileWAL


@pytest.fixture
def switch(tmp_path):
    ks = KillSwitch(FileSwitchStore(tmp_path / "s.json"), cache_ttl=0)
    set_kill_switch(ks)
    yield ks
    set_kill_switch(None)


def a_call(tool="stripe.charge", **kwargs):
    return GateContext(tool=tool, semantics=ActionSemantics.COMPENSABLE,
                       kwargs=kwargs or {"amount": 100})


# ---------------------------------------------------------------------------
# 1. Stopping
# ---------------------------------------------------------------------------

@aio
async def test_a_global_halt_refuses_everything(switch):
    gate = PreFlightGate(rules=[])
    assert await gate.evaluate(a_call())

    switch.halt(reason="incident 4471", by="cto@corp")
    with pytest.raises(PreFlightViolation) as excinfo:
        await gate.evaluate(a_call())
    assert "incident 4471" in excinfo.value.decision.reason
    assert "cto@corp" in excinfo.value.decision.reason


@aio
async def test_a_scoped_halt_stops_only_what_it_names(switch):
    """An operator who can only stop everything will hesitate to stop
    anything."""
    gate = PreFlightGate(rules=[])
    switch.halt(scope="tool:wire.send", reason="fraud pattern", by="soc@corp")

    await gate.evaluate(a_call("stripe.charge"))
    with pytest.raises(PreFlightViolation):
        await gate.evaluate(a_call("wire.send"))


@aio
async def test_a_prefix_halt_stops_a_whole_connector(switch):
    gate = PreFlightGate(rules=[])
    switch.halt(scope="tool:stripe.*", reason="stripe incident", by="soc@corp")

    with pytest.raises(PreFlightViolation):
        await gate.evaluate(a_call("stripe.charge"))
    await gate.evaluate(a_call("salesforce.patch"))


def test_mcp_style_names_resolve_to_a_prefix(switch):
    assert "tool:stripe.*" in switch.scopes_for("stripe__create_charge")
    assert "tool:stripe.*" in switch.scopes_for("stripe.charge")


def test_tag_scopes_apply(switch):
    switch.halt(scope="tag:eu", reason="dpa review", by="dpo@corp")
    with pytest.raises(Halted):
        switch.check_step("anything", tags=("eu",))
    switch.check_step("anything", tags=("us",))


@aio
async def test_resume_lifts_it(switch):
    gate = PreFlightGate(rules=[])
    switch.halt(reason="x", by="a@corp")
    with pytest.raises(PreFlightViolation):
        await gate.evaluate(a_call())
    assert switch.resume(by="a@corp")
    await gate.evaluate(a_call())


def test_resuming_something_not_halted_reports_it(switch):
    assert switch.resume(scope="tool:nope") is False


def test_a_halt_can_expire_on_its_own(switch):
    """A halt nobody remembers to lift is its own outage."""
    switch.halt(reason="deploy", by="ci@corp", ttl=0.5)
    with pytest.raises(Halted):
        switch.check_step("stripe.charge")
    time.sleep(0.55)
    switch.check_step("stripe.charge")


# ---------------------------------------------------------------------------
# 2. Drain
# ---------------------------------------------------------------------------

def test_drain_stops_new_sagas_but_not_running_steps(switch):
    """Blocking the remaining steps of in-flight sagas would strand every one
    of them half-done -- the opposite of draining."""
    switch.halt(reason="deploying", by="ci@corp", drain=True)

    with pytest.raises(Halted):
        switch.check_start()
    switch.check_step("stripe.charge")          # in-flight work continues


def test_a_full_halt_stops_new_sagas_too(switch):
    switch.halt(reason="incident", by="cto@corp")
    with pytest.raises(Halted):
        switch.check_start()
    with pytest.raises(Halted):
        switch.check_step("stripe.charge")


@aio
async def test_saga_context_refuses_to_begin_while_draining(switch, tmp_path):
    from agent_saga.context import SagaContext

    wal = FileWAL(tmp_path / "w.jsonl")
    await wal.start()
    switch.halt(reason="deploying", by="ci@corp", drain=True)
    with pytest.raises(Halted):
        await SagaContext(wal=wal).begin()
    await wal.close()


# ---------------------------------------------------------------------------
# 3. Quarantine
# ---------------------------------------------------------------------------

@aio
async def test_a_quarantined_saga_makes_no_further_calls(switch):
    from agent_saga.observability import set_saga_id

    gate = PreFlightGate(rules=[])
    switch.quarantine("saga-bad", reason="suspected duplicate charges",
                      by="soc@corp")
    set_saga_id("saga-bad")
    try:
        with pytest.raises(PreFlightViolation) as excinfo:
            await gate.evaluate(a_call())
        assert "duplicate charges" in excinfo.value.decision.reason
        set_saga_id("saga-fine")
        await gate.evaluate(a_call())
    finally:
        set_saga_id(None)


def test_quarantine_is_a_freeze_not_a_rollback(switch):
    """Automatically reversing a hundred sagas during an incident can be far
    worse than leaving them still."""
    switch.quarantine("saga-1", reason="investigating", by="soc@corp")
    assert switch.is_quarantined("saga-1")
    # Nothing was compensated; the saga is simply stopped.
    assert switch.status()["quarantined"]["saga-1"]["by"] == "soc@corp"


def test_release_unfreezes(switch):
    switch.quarantine("saga-1", reason="x", by="a@corp")
    assert switch.release("saga-1", by="a@corp")
    assert not switch.is_quarantined("saga-1")
    assert switch.release("saga-1") is False


@aio
async def test_the_recovery_daemon_skips_a_quarantined_saga(switch, tmp_path):
    """The daemon is the one thing that would undo the freeze -- it would
    compensate exactly the saga a human deliberately stopped."""
    from agent_saga.recovery import RecoveryDaemon, Resolution

    wal = FileWAL(tmp_path / "w.jsonl")
    await wal.start()
    wal.append("SAGA_START", {"saga_id": "saga-q", "pid": 1, "lease_ttl": 0.01})
    wal.append("STEP_COMMITTED", {
        "saga_id": "saga-q", "step_id": "s1", "tool": "stripe.charge",
        "semantics": "COMPENSABLE",
        "compensation": {"handler": "nope.handler", "kwargs": {},
                         "recoverable": True, "idempotency_key": "k"}})
    await wal.barrier()
    await wal.close()

    switch.quarantine("saga-q", reason="under investigation", by="soc@corp")
    time.sleep(0.05)

    daemon = RecoveryDaemon(str(tmp_path / "w.jsonl"),
                            claims_dir=str(tmp_path / "claims"))
    dangling = [s for s in await daemon.dangling_async() if s.saga_id == "saga-q"]
    assert dangling, "the saga should look dangling to the daemon"
    outcome = await daemon.recover(dangling[0])

    assert outcome.resolution is Resolution.NEEDS_HUMAN
    assert "quarantined" in outcome.reason


# ---------------------------------------------------------------------------
# 4. When the switch's own store fails
# ---------------------------------------------------------------------------

class FlakyStore:
    distributed = True

    def __init__(self):
        self.up = True
        self.switches = {}

    def read(self):
        if not self.up:
            raise ConnectionError("redis is down")
        return dict(self.switches)

    def write(self, switch):
        self.switches[switch.scope] = switch

    def clear(self, scope):
        return self.switches.pop(scope, None) is not None

    def quarantine(self, saga_id, reason, by):
        pass

    def quarantined(self):
        if not self.up:
            raise ConnectionError("redis is down")
        return {}

    def release(self, saga_id):
        return False


def test_a_blip_is_survived_using_the_last_known_state():
    """Failing closed the instant the store hiccups would make the kill switch
    the largest availability risk in the system."""
    store = FlakyStore()
    ks = KillSwitch(store, grace=60, cache_ttl=0)
    ks.check_step("stripe.charge")              # primes the cache: not halted

    store.up = False
    ks.check_step("stripe.charge")              # blip survived


def test_a_sustained_outage_eventually_fails_closed():
    """An outage must not become an indefinite bypass for anyone who can take
    the store down."""
    store = FlakyStore()
    ks = KillSwitch(store, grace=0.1, cache_ttl=0)
    ks.check_step("stripe.charge")

    store.up = False
    ks.check_step("stripe.charge")              # inside the grace window
    time.sleep(0.15)
    with pytest.raises(Halted) as excinfo:
        ks.check_step("stripe.charge")
    assert "unreadable" in str(excinfo.value)


def test_a_cached_halt_still_applies_while_degraded():
    store = FlakyStore()
    ks = KillSwitch(store, grace=60, cache_ttl=0)
    ks.halt(reason="incident", by="a@corp")
    ks.check_start.__self__  # noqa: B018 - keep the reference explicit
    with pytest.raises(Halted):
        ks.check_step("stripe.charge")

    store.up = False
    with pytest.raises(Halted):
        ks.check_step("stripe.charge")


def test_an_unprovable_state_treats_sagas_as_quarantined():
    """The daemon must not compensate a saga it cannot prove is free to touch."""
    store = FlakyStore()
    ks = KillSwitch(store, grace=0, cache_ttl=0)
    store.up = False
    assert ks.is_quarantined("saga-1") is True


def test_a_corrupt_switch_file_does_not_read_as_running(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    ks = KillSwitch(FileSwitchStore(path), grace=0, cache_ttl=0)
    with pytest.raises(Halted):
        ks.check_step("stripe.charge")


# ---------------------------------------------------------------------------
# 5. Ordering and audit
# ---------------------------------------------------------------------------

@aio
async def test_a_halted_call_does_not_consume_budget(switch):
    """A halted system must not spend budget deciding to refuse."""
    store = InProcessLimitStore()
    set_limit_store(store)
    try:
        gate = PreFlightGate(rules=[], limits=[
            BudgetLimit("daily", arg="amount", max_total=1_000, window=60)])
        switch.halt(reason="incident", by="a@corp")
        for _ in range(5):
            with pytest.raises(PreFlightViolation):
                await gate.evaluate(a_call(amount=100))
        assert store.usage("daily::*", 60) == 0
    finally:
        set_limit_store(InProcessLimitStore())


@aio
async def test_a_halted_call_does_not_wake_a_human(switch):
    asked = []
    gate = PreFlightGate(approval_provider=lambda c, r: asked.append(1) or True)
    switch.halt(reason="incident", by="a@corp")
    with pytest.raises(PreFlightViolation):
        await gate.evaluate(GateContext("wire.send", ActionSemantics.IRREVERSIBLE,
                                        {"amount": 1}))
    assert asked == []


@aio
async def test_halts_are_recorded_in_the_tamper_evident_log(tmp_path):
    from agent_saga.integrity import verify

    wal = FileWAL(tmp_path / "w.jsonl")
    await wal.start()
    ks = KillSwitch(FileSwitchStore(tmp_path / "s.json"), wal=wal, cache_ttl=0)
    ks.halt(reason="incident 4471", by="cto@corp")
    ks.quarantine("saga-1", reason="investigating", by="soc@corp")
    ks.resume(by="cto@corp")
    await wal.barrier()
    records = wal.records()
    await wal.close()

    events = [r["event"] for r in records]
    assert events == ["KILLSWITCH_ENGAGED", "SAGA_QUARANTINED", "KILLSWITCH_RELEASED"]
    assert records[0]["by"] == "cto@corp" and records[0]["reason"] == "incident 4471"
    assert verify(records).intact


def test_status_reports_what_is_halted(switch):
    switch.halt(scope="tool:wire.send", reason="fraud", by="soc@corp")
    switch.quarantine("saga-1", reason="investigating", by="soc@corp")
    status = switch.status()
    assert status["reachable"] is True
    assert any("wire.send" in s for s in status["switches"])
    assert "saga-1" in status["quarantined"]


def test_installing_a_local_store_warns_that_it_is_not_a_fleet(caplog, tmp_path):
    """An operator must not believe they stopped a fleet they did not."""
    import logging

    with caplog.at_level(logging.WARNING):
        set_kill_switch(KillSwitch(FileSwitchStore(tmp_path / "s.json")))
    set_kill_switch(None)
    assert any("not distributed" in r.message for r in caplog.records)


def test_no_switch_installed_means_no_overhead():
    set_kill_switch(None)
    assert get_kill_switch() is None


def test_switch_serialisation_round_trips():
    original = Switch(scope="tool:x", state=DRAINING, reason="r", by="b",
                      expires_at=123.0)
    assert Switch.from_dict(json.loads(json.dumps(original.to_dict()))) == original


# ---------------------------------------------------------------------------
# 6. CLI
# ---------------------------------------------------------------------------

def test_cli_halt_resume_status(capsys, tmp_path):
    from agent_saga.cli import main

    path = str(tmp_path / "s.json")
    assert main(["status", "--file", path]) == 0
    assert "RUNNING" in capsys.readouterr().out

    assert main(["halt", "--file", path, "--reason", "incident 4471",
                 "--by", "cto@corp"]) == 0
    out = capsys.readouterr().out
    assert "HALTED" in out and "not distributed" in out

    assert main(["status", "--file", path]) == 0
    assert "incident 4471" in capsys.readouterr().out

    assert main(["resume", "--file", path, "--by", "cto@corp"]) == 0
    assert main(["status", "--file", path]) == 0
    assert "RUNNING" in capsys.readouterr().out


def test_cli_halt_requires_who_and_why(capsys, tmp_path):
    from agent_saga.cli import main

    assert main(["halt", "--file", str(tmp_path / "s.json"),
                 "--reason", "x"]) == 2
    assert "--by" in capsys.readouterr().out


def test_cli_quarantine_says_it_is_not_a_rollback(capsys, tmp_path):
    from agent_saga.cli import main

    path = str(tmp_path / "s.json")
    assert main(["quarantine", "saga-1", "--file", path, "--reason", "dupes",
                 "--by", "soc@corp"]) == 0
    out = capsys.readouterr().out
    assert "not a rollback" in out

    assert main(["quarantine", "saga-1", "--file", path, "--release"]) == 0
    assert "released" in capsys.readouterr().out


def test_cli_resume_reports_nothing_to_lift(capsys, tmp_path):
    from agent_saga.cli import main

    assert main(["resume", "--file", str(tmp_path / "s.json")]) == 1
    assert "nothing halted" in capsys.readouterr().out
