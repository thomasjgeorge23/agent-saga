"""Declarative inverses: less ceremony, none of the guarantees traded away.

The friction this addresses is real -- a compensation factory per tool, mostly
the same shape each time. What must NOT be traded for that convenience:

1. Semantics are still declared by the author, never inferred.
2. An inverse must be registry-backed, or pairing fails loudly at import --
   a closure cannot be run by saga-recoveryd after a crash.
3. A missing result field raises with what it wanted and what it saw, rather
   than compensating with None.
4. An UNKNOWN forward outcome does not produce an invented compensation.
5. An explicit compensate= always wins over a declaration.
"""

import pytest
from conftest import aio

from agent_saga import ActionSemantics, AgentKit, AsyncWAL, SagaAborted, saga_scope
from agent_saga.inverses import (
    InverseError,
    auto_compensation,
    call_with,
    delete_by,
    has_inverse,
    inverse_of,
    map_result,
)
from agent_saga.registry import compensator

RELEASED = []


# -- the declaration, once, next to the inverse --------------------------------

def create_instance(image: str) -> dict:
    return {"InstanceId": f"i-{image}", "image": image}


@inverse_of(create_instance, maps={"instance_id": "InstanceId"})
@compensator("test.terminate_instance")
def terminate_instance(instance_id: str) -> dict:
    RELEASED.append(instance_id)
    return {"terminated": instance_id}


@pytest.fixture(autouse=True)
def _clear():
    RELEASED.clear()
    yield
    RELEASED.clear()


# -- 1 & 5. pairing is found, and an explicit factory still wins -----------------

def test_the_pairing_is_registered():
    assert has_inverse(create_instance)
    assert auto_compensation(create_instance) is not None
    assert terminate_instance.__inverse_of__ is create_instance


@aio
async def test_safe_tool_uses_the_declaration_with_no_compensate_argument(tmp_path):
    kit = AgentKit(name="t")
    launch = kit.safe_tool(create_instance, semantics="COMPENSABLE")

    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        with pytest.raises(SagaAborted):
            async with saga_scope(wal=wal) as saga:
                await saga.execute(
                    tool="create_instance", semantics=ActionSemantics.COMPENSABLE,
                    forward=create_instance, forward_kwargs={"image": "ami1"},
                    compensate=auto_compensation(create_instance))

                def boom():
                    raise RuntimeError("later step failed")
                await saga.execute(tool="boom",
                                   semantics=ActionSemantics.COMPENSABLE, forward=boom)
    finally:
        await wal.close()

    assert RELEASED == ["i-ami1"], "the declared inverse did not run"


def test_semantics_are_never_inferred():
    """The one judgement the engine refuses to make. A declared inverse must
    not imply COMPENSABLE."""
    kit = AgentKit(name="t")
    with pytest.raises(TypeError):
        kit.safe_tool(create_instance)          # semantics is keyword-required


@aio
async def test_an_explicit_compensate_wins_over_the_declaration(tmp_path):
    override = []

    @compensator("test.override")
    def other(instance_id: str):
        override.append(instance_id)

    kit = AgentKit(name="t")
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        with pytest.raises(SagaAborted):
            async with saga_scope(wal=wal) as saga:
                await saga.execute(
                    tool="create_instance", semantics=ActionSemantics.COMPENSABLE,
                    forward=create_instance, forward_kwargs={"image": "ami2"},
                    compensate=map_result(other, {"instance_id": "InstanceId"}))
                raise RuntimeError("abort")
    finally:
        await wal.close()

    assert override == ["i-ami2"]
    assert RELEASED == []


# -- 2. the inverse must survive a crash -------------------------------------------

def test_pairing_an_unregistered_function_fails_at_declaration():
    """A closure cannot be run by a recovery daemon. Better to learn that at
    import than at 3am."""
    def forward():
        return {}

    def not_registered(x):
        return x

    with pytest.raises(InverseError, match="not a registered compensation handler"):
        inverse_of(forward, maps={"x": "id"})(not_registered)


def test_the_generated_compensation_is_recoverable():
    factory = auto_compensation(create_instance)
    compensation = factory({"InstanceId": "i-9"})
    assert compensation.handler == "test.terminate_instance"
    assert compensation.kwargs == {"instance_id": "i-9"}
    assert compensation.recoverable is True     # JSON kwargs + a handler name


def test_two_inverses_for_one_forward_are_refused():
    def forward():
        return {}

    @compensator("test.first_inverse")
    def first(x):
        return x

    @compensator("test.second_inverse")
    def second(x):
        return x

    inverse_of(forward, maps={"x": "id"})(first)
    with pytest.raises(InverseError, match="already has an inverse"):
        inverse_of(forward, maps={"x": "id"})(second)


# -- 3 & 4. it refuses rather than compensating with nothing --------------------------

def test_a_missing_result_field_names_what_it_wanted_and_what_it_saw():
    factory = auto_compensation(create_instance)
    with pytest.raises(InverseError) as excinfo:
        factory({"WrongKey": "i-1"})
    message = str(excinfo.value)
    assert "InstanceId" in message and "WrongKey" in message
    assert "Refusing to compensate" in message


def test_an_unknown_outcome_yields_no_invented_compensation():
    """result is None when the forward call raised or timed out. A mapped
    inverse cannot know the id, so it declines and the step is ORPHANED --
    which is true -- instead of refunding a guess."""
    assert auto_compensation(create_instance)(None) is None


def test_call_with_stays_valid_on_an_unknown_outcome():
    """Its target was known before the forward call, so it is correct whether
    the effect landed, half-landed, or raised."""
    calls = []

    @compensator("test.restore_flag")
    def restore(flag: str):
        calls.append(flag)

    factory = call_with(restore, flag="maintenance")
    compensation = factory(None)                # UNKNOWN outcome
    assert compensation is not None
    assert compensation.kwargs == {"flag": "maintenance"}
    compensation.fn(**compensation.kwargs)
    assert calls == ["maintenance"]


# -- the ergonomic shorthands ------------------------------------------------------------

def test_delete_by_covers_the_common_create_then_delete_shape():
    removed = []

    @compensator("test.delete_row")
    def delete_row(id: str):
        removed.append(id)

    factory = delete_by(delete_row)
    compensation = factory({"id": "row-7"})
    assert compensation.kwargs == {"id": "row-7"}
    assert compensation.handler == "test.delete_row"


def test_static_arguments_are_merged():
    @compensator("test.refund_partial")
    def refund(charge_id: str, reason: str):
        return (charge_id, reason)

    factory = map_result(refund, {"charge_id": "id"}, static={"reason": "saga-rollback"})
    compensation = factory({"id": "ch_1"})
    assert compensation.kwargs == {"charge_id": "ch_1", "reason": "saga-rollback"}


def test_the_description_is_useful_by_default():
    factory = auto_compensation(create_instance)
    assert "test.terminate_instance" in factory({"InstanceId": "i-1"}).description
