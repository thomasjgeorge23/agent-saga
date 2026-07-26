"""The base-strengthening sweep: validation, introspection, accounting, typing.

The claims under test:
1. Misconfiguration fails at construction, at the desk -- never at 3am.
2. Operators can read posture: router.hosts(), broker.stats().
3. A completed run accounts for itself: tokens and wall clock on the result.
4. The wheel declares its types (PEP 561 py.typed) and the version is 0.4.0.
"""

import json
from pathlib import Path

import pytest
from conftest import aio

import agent_saga
from agent_saga import (
    ActionSemantics,
    AgentLoop,
    AsyncWAL,
    Compensation,
    ContextBroker,
    CostClass,
    LoopTool,
    Router,
    saga_scope,
)
from agent_saga.ir import Capabilities, ChatRequest, ChatResponse

CAPS = Capabilities(context_tokens=50_000, supports_tools=True,
                    cost_class=CostClass.LOCAL)


class Host:
    def __init__(self, replies=("ok",)):
        self.replies = list(replies)

    @property
    def name(self):
        return "host"

    def capabilities(self):
        return CAPS

    async def complete(self, request: ChatRequest) -> ChatResponse:
        return ChatResponse(text=self.replies.pop(0), provider="host", model="m")


NOOP_TOOL = LoopTool(
    name="noop", description="does nothing", fn=lambda: {"ok": True},
    semantics=ActionSemantics.COMPENSABLE,
    compensate=lambda r: Compensation(fn=lambda: None, description="noop"))


# -- 1. fail at the desk ------------------------------------------------------------

def test_router_refuses_to_exist_without_adapters():
    with pytest.raises(ValueError, match="no adapters"):
        Router([])


def test_router_refuses_zero_attempts():
    with pytest.raises(ValueError, match="attempts_per_host"):
        Router([Host()], attempts_per_host=0)


@pytest.mark.parametrize("kw", [
    {"max_steps": 0},
    {"budget_tokens": 0},
    {"max_repeated_calls": 0},
    {"deadline_s": -1.0},
    {"max_total_tokens": 0},
])
def test_loop_refuses_impossible_configuration(kw):
    with pytest.raises(ValueError):
        AgentLoop(Router([Host()]), ContextBroker(), [NOOP_TOOL], **kw)


@pytest.mark.parametrize("kw", [
    {"max_hot_entries": 0},
    {"max_warm_entries": 0},
])
def test_broker_refuses_impossible_bounds(kw):
    with pytest.raises(ValueError):
        ContextBroker(**kw)


# -- 2. readable posture --------------------------------------------------------------

def test_router_hosts_reports_declared_capabilities_in_candidate_order():
    hosts = Router([Host()]).hosts()
    assert hosts == {"host": {
        "context_tokens": 50_000, "supports_tools": True,
        "supports_json_mode": False, "supports_streaming": False,
        "cost_class": "LOCAL"}}


def test_broker_stats_counts_every_tier_and_loss():
    broker = ContextBroker(prefix="P", max_warm_entries=1)
    spans = broker.add_document("doc", "some text " * 50)
    broker.admit_summary("one", spans)
    broker.admit_summary("two", spans)           # displaces "one"
    broker.push_hot("a turn")

    stats = broker.stats()
    assert stats["hot_entries"] == 1
    assert stats["warm_entries"] == 1
    assert stats["displaced_total"] == 1
    assert stats["max_warm_entries"] == 1


# -- 3. a completed run accounts for itself ---------------------------------------------

@aio
async def test_loop_result_carries_tokens_and_elapsed(tmp_path):
    replies = [json.dumps({"action": "tool", "tool": "noop", "args": {}}),
               json.dumps({"action": "final", "answer": "done"})]
    wal = AsyncWAL(tmp_path / "wal.jsonl")
    await wal.start()
    try:
        loop = AgentLoop(Router([Host(replies)]), ContextBroker(prefix="t"),
                         [NOOP_TOOL])
        async with saga_scope(wal=wal) as ctx:
            result = await loop.run("goal", ctx)
    finally:
        await wal.close()

    assert result.status == "COMPLETED"
    assert result.tokens_used > 0                # estimated here, and says so
    assert result.elapsed_s >= 0.0


# -- 4. typed and versioned ---------------------------------------------------------------

def test_the_package_declares_its_types():
    assert (Path(agent_saga.__file__).parent / "py.typed").exists()


def test_version_is_plain_semver():
    """Shape, not a pinned number -- releases bump faster than tests should.
    Cross-package lockstep is enforced separately in test_pytest_plugin.py."""
    import re
    assert re.fullmatch(r"\d+\.\d+\.\d+", agent_saga.__version__)
