"""Verification-gated cascade: the cheapest tier that can be proven right.

The claim is narrow and testable. It is NOT "a weak model becomes a strong
one" -- that is not deliverable by any middleware. It is:

  * an answer is returned only if a check that can actually fail passed it;
  * when the cheap tier suffices, nothing more expensive is called;
  * when it does not, the next tier receives the *specific* rejection reason;
  * and the cost of every tier, including the rejected ones, is reported --
    so a cascade that is not saving anything says so.
"""

import json

import pytest
from conftest import aio

from agent_saga import AsyncWAL, saga_scope
from agent_saga.cascade import CascadeExhausted, cascade, tools_must_exist
from agent_saga.ir import (Capabilities, ChatRequest, ChatResponse, CostClass,
                           ToolCall, Usage, user)

SMALL = Capabilities(context_tokens=8_000, cost_class=CostClass.LOCAL)
BIG = Capabilities(context_tokens=200_000, cost_class=CostClass.PREMIUM)


class Host:
    """Scripted host that records exactly what it was asked."""

    def __init__(self, name, replies, caps=SMALL, usage=None, fail=False):
        self._name, self._caps = name, caps
        self.replies = list(replies)
        self.requests = []
        self.usage = usage
        self.fail = fail

    @property
    def name(self):
        return self._name

    def capabilities(self):
        return self._caps

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if self.fail:
            raise ConnectionError(f"{self._name} unreachable")
        text = self.replies.pop(0) if self.replies else "{}"
        return ChatResponse(text=text, provider=self._name, model="m",
                            usage=self.usage or Usage())


def must_be_json(response: ChatResponse) -> None:
    json.loads(response.text)


def req(text="do the thing"):
    return ChatRequest(messages=(user(text),))


# -- the cheap win --------------------------------------------------------------

@aio
async def test_a_verified_cheap_answer_never_touches_the_expensive_tier():
    small = Host("local-7b", ['{"ok": true}'])
    big = Host("frontier", ['{"ok": true}'], caps=BIG)

    result = await cascade(req(), ladder=[small, big], verify=must_be_json)

    assert result.host == "local-7b"
    assert result.resolved_at_first_tier
    assert result.escalations == 0
    assert big.requests == [], "the expensive host was called unnecessarily"


@aio
async def test_same_host_repair_happens_before_any_escalation():
    """A weak model given the precise failure often fixes it, and that is far
    cheaper than a frontier call."""
    small = Host("local-7b", ["not json", '{"fixed": true}'])
    big = Host("frontier", ['{"ok": true}'], caps=BIG)

    result = await cascade(req(), ladder=[small, big], verify=must_be_json,
                           repair_rounds=1)

    assert result.host == "local-7b"
    assert len(small.requests) == 2
    assert big.requests == []
    assert result.rungs[0].attempts == 2


# -- escalation carries the reason ------------------------------------------------

@aio
async def test_escalation_passes_the_specific_rejection_reason_upward():
    """The payload is the reason. A retry that does not say what was wrong is
    just a second roll of the same dice."""
    small = Host("local-7b", ["still not json", "nope again"])
    big = Host("frontier", ['{"ok": true}'], caps=BIG)

    result = await cascade(req(), ladder=[small, big], verify=must_be_json,
                           repair_rounds=1)

    assert result.host == "frontier"
    assert result.escalations == 1

    escalated = big.requests[0].messages[-1].content
    assert "rejected by an automated check" in escalated
    assert "json" in escalated.lower() or "Expecting value" in escalated


@aio
async def test_every_rung_records_why_it_was_rejected():
    small = Host("local-7b", ["bad"])
    big = Host("frontier", ['{"ok": true}'], caps=BIG)

    result = await cascade(req(), ladder=[small, big], verify=must_be_json,
                           repair_rounds=0)

    first, second = result.rungs
    assert first.host == "local-7b" and not first.accepted and first.reason
    assert second.host == "frontier" and second.accepted and second.reason is None


@aio
async def test_a_host_that_raises_is_a_rejection_not_a_crash():
    broken = Host("flaky", [], fail=True)
    big = Host("frontier", ['{"ok": true}'], caps=BIG)

    result = await cascade(req(), ladder=[broken, big], verify=must_be_json)
    assert result.host == "frontier"
    assert "host raised" in result.rungs[0].reason


# -- nothing unverified is ever returned --------------------------------------------

@aio
async def test_exhaustion_raises_rather_than_returning_an_unverified_answer():
    """Returning the last answer anyway would make the whole cascade
    decorative."""
    small = Host("local-7b", ["bad", "bad"])
    big = Host("frontier", ["also bad", "still bad"], caps=BIG)

    with pytest.raises(CascadeExhausted) as excinfo:
        await cascade(req(), ladder=[small, big], verify=must_be_json)

    assert len(excinfo.value.rungs) == 2
    assert all(not r.accepted for r in excinfo.value.rungs)
    assert "local-7b" in str(excinfo.value) and "frontier" in str(excinfo.value)


def test_an_empty_ladder_is_refused():
    with pytest.raises(ValueError, match="at least one host"):
        import asyncio
        asyncio.run(cascade(req(), ladder=[], verify=must_be_json))


# -- cost is measured, not asserted ---------------------------------------------------

@aio
async def test_tokens_from_rejected_tiers_are_counted_too():
    """Counting only the winning tier would flatter the cascade."""
    small = Host("local-7b", ["bad"], usage=Usage(input_tokens=100, output_tokens=10))
    big = Host("frontier", ['{"ok": true}'], caps=BIG,
               usage=Usage(input_tokens=500, output_tokens=50))

    result = await cascade(req(), ladder=[small, big], verify=must_be_json,
                           repair_rounds=0)

    assert result.rungs[0].tokens == 110
    assert result.rungs[1].tokens == 550
    assert result.tokens_used == 660          # not 550
    assert "total tokens across all tiers: 660" in result.format_text()


@aio
async def test_the_report_is_machine_readable():
    small = Host("local-7b", ['{"ok": true}'])
    result = await cascade(req(), ladder=[small], verify=must_be_json)

    data = result.describe()
    assert data["host"] == "local-7b"
    assert data["escalations"] == 0
    assert data["resolved_at_first_tier"] is True
    assert len(data["rungs"]) == 1


# -- the built-in verifier worth having -------------------------------------------------

@aio
async def test_a_hallucinated_tool_name_escalates_instead_of_crashing():
    """The commonest weak-model failure in an agent loop, and one a check can
    settle absolutely: 'does this tool exist?' has exactly one right answer."""
    class ToolHost(Host):
        async def complete(self, request):
            self.requests.append(request)
            name = self.replies.pop(0)
            return ChatResponse(text="", provider=self._name, model="m",
                                tool_calls=(ToolCall(id="1", name=name),))

    small = ToolHost("local-7b", ["send_emial", "send_emial"])   # typo'd tool
    big = ToolHost("frontier", ["send_email"], caps=BIG)

    verify = tools_must_exist(["send_email", "search"])
    result = await cascade(req(), ladder=[small, big], verify=verify)

    assert result.host == "frontier"
    assert "do not exist" in result.rungs[0].reason
    assert "send_emial" in result.rungs[0].reason
    assert "send_email" in result.rungs[0].reason      # names what IS available


# -- the ladder trace lands in the WAL ---------------------------------------------------

@aio
async def test_the_whole_ladder_including_failures_is_audited(tmp_path):
    small = Host("local-7b", ["bad"])
    big = Host("frontier", ['{"ok": true}'], caps=BIG)

    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        async with saga_scope(wal=wal, name="cascade") as ctx:
            await cascade(req(), ladder=[small, big], verify=must_be_json,
                          repair_rounds=0, ctx=ctx)
        events = [r for r in await wal.read_all()
                  if r.get("event") == "CASCADE_RESOLVED"]
    finally:
        await wal.close()

    assert len(events) == 1
    assert events[0]["resolved_host"] == "frontier"
    assert events[0]["escalations"] == 1
    assert len(events[0]["rungs"]) == 2
    assert events[0]["rungs"][0]["reason"]            # the failure is on the log


@aio
async def test_an_exhausted_cascade_is_still_audited(tmp_path):
    """Especially the total failure -- that is the run somebody will ask about."""
    small = Host("local-7b", ["bad"])

    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        async with saga_scope(wal=wal, name="cascade") as ctx:
            with pytest.raises(CascadeExhausted):
                await cascade(req(), ladder=[small], verify=must_be_json,
                              repair_rounds=0, ctx=ctx)
        events = [r for r in await wal.read_all()
                  if r.get("event") == "CASCADE_RESOLVED"]
    finally:
        await wal.close()

    assert len(events) == 1
    assert events[0]["resolved_host"] is None
