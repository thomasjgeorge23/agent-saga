"""IR + Router: the truthful-report invariant applied to routing.

The claims under test:
1. Routing is deterministic -- same request, policy, adapters, breaker state,
   same decision.
2. Refusal is never bare: NoCapableHost / AllHostsFailed name every candidate
   and every reason.
3. Fallback is visible: runtime failures are recorded on the decision and fed
   to the breaker; an open circuit is a named rejection.
4. Validation repair is bounded, recorded, and never falls back to a different
   host -- an unacceptable answer is not a routing problem.
5. The final decision, including the failures, lands in the WAL when a saga
   context is supplied.
"""

import json

import pytest
from conftest import aio

from agent_saga import AsyncWAL, saga_scope
from agent_saga.breaker import BreakerPolicy, CircuitBreaker, set_breaker
from agent_saga.ir import Capabilities, ChatRequest, ChatResponse, CostClass, ToolSpec, user
from agent_saga.router import (
    AllHostsFailed,
    NoCapableHost,
    Router,
    RoutingPolicy,
    ValidationExhausted,
)

BIG = Capabilities(context_tokens=200_000, supports_tools=True,
                   supports_json_mode=True, cost_class=CostClass.PREMIUM)
SMALL = Capabilities(context_tokens=1_000, cost_class=CostClass.LOCAL)


class FakeHost:
    """Deterministic HostAdapter: scripted replies, optional hard failure."""

    def __init__(self, name, caps, replies=None, fail=False):
        self._name, self._caps = name, caps
        self.calls = []
        self.replies = list(replies or [])
        self.fail = fail

    @property
    def name(self):
        return self._name

    def capabilities(self):
        return self._caps

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.calls.append(request)
        if self.fail:
            raise ConnectionError(f"{self._name} unreachable")
        text = self.replies.pop(0) if self.replies else "ok"
        return ChatResponse(text=text, provider=self._name, model="fake-1",
                            provider_extra=request.provider_extra)


@pytest.fixture(autouse=True)
def _no_global_breaker():
    set_breaker(None)
    yield
    set_breaker(None)


def req(text="hello", **kw) -> ChatRequest:
    return ChatRequest(messages=(user(text),), **kw)


# -- 1. determinism ---------------------------------------------------------------

def test_routing_is_deterministic():
    router = Router([FakeHost("a", BIG), FakeHost("b", BIG)],
                    RoutingPolicy(prefer=("b", "a")))
    first = router.route(req())
    second = router.route(req())
    assert first.adapter == second.adapter == "b"
    assert first.considered == ("b", "a")


# -- 2. refusal is never bare -------------------------------------------------------

def test_no_capable_host_names_every_candidate_and_reason():
    router = Router([FakeHost("tiny", SMALL), FakeHost("older", SMALL)])
    request = req(json_only=True,
                  tools=(ToolSpec(name="search", description="find things"),))

    with pytest.raises(NoCapableHost) as excinfo:
        router.route(request)

    rejections = excinfo.value.rejections
    assert set(rejections) == {"tiny", "older"}
    for reasons in rejections.values():
        assert any("supports_tools=False" in r for r in reasons)
        assert any("supports_json_mode=False" in r for r in reasons)


def test_big_payload_routes_past_the_small_context_host():
    router = Router([FakeHost("tiny", SMALL), FakeHost("big", BIG)],
                    RoutingPolicy(prefer=("tiny", "big")))
    decision = router.route(req("x" * 40_000))     # ~10k estimated tokens
    assert decision.adapter == "big"
    assert any("exceeds declared context" in r for r in decision.rejections["tiny"])


def test_duplicate_adapter_names_are_refused():
    with pytest.raises(ValueError, match="unique"):
        Router([FakeHost("same", BIG), FakeHost("same", SMALL)])


# -- 3. fallback is visible ---------------------------------------------------------

@aio
async def test_fallback_serves_from_the_next_host_and_records_the_failure():
    flaky, healthy = FakeHost("flaky", BIG, fail=True), FakeHost("healthy", BIG)
    router = Router([flaky, healthy], RoutingPolicy(prefer=("flaky", "healthy")))

    response, decision = await router.complete(req())
    assert response.provider == "healthy"
    assert decision.adapter == "healthy"
    assert decision.attempts == ["flaky", "healthy"]
    assert any("raised at call time" in r for r in decision.rejections["flaky"])


@aio
async def test_all_hosts_failed_carries_every_reason_and_the_cause():
    router = Router([FakeHost("a", BIG, fail=True), FakeHost("b", BIG, fail=True)])
    with pytest.raises(AllHostsFailed) as excinfo:
        await router.complete(req())
    assert set(excinfo.value.rejections) == {"a", "b"}
    assert isinstance(excinfo.value.__cause__, ConnectionError)


@aio
async def test_an_open_circuit_is_a_named_rejection_not_a_silent_skip():
    set_breaker(CircuitBreaker(BreakerPolicy(failure_threshold=2)))
    flaky, healthy = FakeHost("flaky", BIG, fail=True), FakeHost("healthy", BIG)
    router = Router([flaky, healthy], RoutingPolicy(prefer=("flaky", "healthy")))

    await router.complete(req())        # failure 1 recorded against llm.flaky
    await router.complete(req())        # failure 2 -- circuit opens
    calls_before = len(flaky.calls)

    decision = router.route(req())
    assert decision.adapter == "healthy"
    assert any("circuit open" in r for r in decision.rejections["flaky"])
    assert len(flaky.calls) == calls_before    # not even attempted


# -- 4. bounded repair, no silent coercion --------------------------------------------

def _must_be_json(response: ChatResponse):
    json.loads(response.text)


@aio
async def test_repair_is_bounded_and_the_error_reaches_the_model():
    host = FakeHost("h", BIG, replies=["not json at all", '{"a": 1}'])
    router = Router([host])

    response, _ = await router.complete(req(), validate=_must_be_json,
                                        max_repair_rounds=1)
    assert response.text == '{"a": 1}'
    assert len(host.calls) == 2
    repair_msg = host.calls[1].messages[-1]
    assert "failed validation" in repair_msg.content
    # the model also sees its own previous (bad) answer
    assert host.calls[1].messages[-2].content == "not json at all"


@aio
async def test_exhausted_repair_raises_with_the_full_error_history():
    host = FakeHost("h", BIG, replies=["bad", "still bad"])
    router = Router([host])

    with pytest.raises(ValidationExhausted) as excinfo:
        await router.complete(req(), validate=_must_be_json, max_repair_rounds=1)
    assert len(excinfo.value.errors) == 2
    assert len(host.calls) == 2                # bounded: exactly 1 repair round


@aio
async def test_validation_failure_never_falls_back_to_another_host():
    """An unacceptable answer from a healthy host is not a routing problem;
    retrying it on a cheaper host would be routing on hope."""
    bad, unused = FakeHost("bad", BIG, replies=["nope", "nope"]), FakeHost("unused", BIG)
    router = Router([bad, unused], RoutingPolicy(prefer=("bad", "unused")))

    with pytest.raises(ValidationExhausted):
        await router.complete(req(), validate=_must_be_json)
    assert unused.calls == []


# -- 5. the decision lands in the WAL ---------------------------------------------------

@aio
async def test_the_routing_decision_is_recorded_in_the_saga_wal(tmp_path):
    flaky, healthy = FakeHost("flaky", BIG, fail=True), FakeHost("healthy", BIG)
    router = Router([flaky, healthy], RoutingPolicy(prefer=("flaky", "healthy")))

    wal = AsyncWAL(tmp_path / "wal.jsonl")
    await wal.start()
    try:
        async with saga_scope(wal=wal) as ctx:
            await router.complete(req(), ctx=ctx)
        routed = [r for r in await wal.read_all() if r.get("event") == "LLM_ROUTED"]
    finally:
        await wal.close()

    assert len(routed) == 1
    assert routed[0]["adapter"] == "healthy"
    assert routed[0]["saga_id"]
    assert any("raised at call time" in r for r in routed[0]["rejections"]["flaky"])


# -- the IR keeps its hands off provider data --------------------------------------------

@aio
async def test_provider_extra_rides_through_untouched():
    host = FakeHost("h", BIG)
    router = Router([host])
    extra = {"vendor_beta_flag": {"nested": [1, 2, 3]}}

    response, _ = await router.complete(req(provider_extra=extra))
    assert host.calls[0].provider_extra == extra
    assert response.provider_extra == extra
