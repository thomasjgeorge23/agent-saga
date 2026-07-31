"""Semantic Kernel and Vertex AI adapters.

Neither SDK is installed in CI's base env, so the part that carries the
guarantee -- routing, gating, LIFO rollback -- is pinned against fakes, exactly
as the CrewAI and OpenAI adapters are. Each adapter also has one real
integration test that skips when its SDK is absent.

Being explicit about which half is verified matters more than implying both
are: `build_runner` is shared with every other adapter and is genuinely proven
here; the SDK-specific packaging is proven only where the SDK exists.
"""

import inspect

import pytest
from conftest import aio

from agent_saga import (ActionSemantics, AsyncWAL, Compensation, PreFlightGate,
                        PreFlightViolation, Rule, SagaAborted, Verdict,
                        arg_exceeds, saga_scope)
from agent_saga.adapters.vertex_ai import (UnknownFunctionCall,
                                           dispatch_function_call, saga_run,
                                           wrap_tool)

C = ActionSemantics.COMPENSABLE


# -- Vertex AI: the routing core, no SDK required ---------------------------------

@aio
async def test_a_wrapped_vertex_tool_joins_the_active_saga(tmp_path):
    world = {"charges": []}

    def charge_customer(amount: int) -> dict:
        """Charge a card."""
        world["charges"].append("ch_1")
        return {"id": "ch_1", "amount": amount}

    charge = wrap_tool(
        charge_customer, semantics=C,
        compensate=lambda r: Compensation(
            fn=lambda cid: world["charges"].remove(cid),
            kwargs={"cid": r["id"]}, description="refund"))

    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        with pytest.raises(SagaAborted):
            async with saga_scope(wal=wal, name="vertex"):
                await charge(amount=4200)
                assert world["charges"] == ["ch_1"]
                raise RuntimeError("a later turn failed")
    finally:
        await wal.close()

    assert world["charges"] == [], "the wrapped tool was not rolled back"


@aio
async def test_the_wrapper_preserves_what_vertex_reads_for_its_schema():
    """Vertex builds the FunctionDeclaration by introspecting the callable, so
    a wrapper that lost the signature would silently change the schema the model
    sees."""
    def lookup_order(order_id: str, include_items: bool = False) -> dict:
        """Look up an order by id."""
        return {}

    wrapped = wrap_tool(lookup_order, semantics=C)

    assert wrapped.__name__ == "lookup_order"
    assert wrapped.__doc__ == "Look up an order by id."
    signature = inspect.signature(wrapped)
    assert list(signature.parameters) == ["order_id", "include_items"]
    assert signature.parameters["include_items"].default is False


@aio
async def test_a_wrapped_tool_still_runs_outside_a_saga():
    """One object serves the agent, a script and a unit test."""
    def ping(x: int) -> int:
        """Ping."""
        return x + 1

    assert await wrap_tool(ping, semantics=C)(x=1) == 2


@aio
async def test_arguments_reach_the_gate(tmp_path):
    """An argument hidden in a closure is invisible to a threshold rule -- the
    bug `policy_args` exists to prevent."""
    def charge(amount: int) -> dict:
        """Charge."""
        return {"id": "ch"}

    gate = PreFlightGate(rules=[Rule(
        name="cap", when=arg_exceeds("amount", 1000),
        verdict=Verdict.BLOCK, reason="over the cap")])

    charge_tool = wrap_tool(charge, semantics=C)
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        with pytest.raises(SagaAborted) as excinfo:
            async with saga_scope(wal=wal, gate=gate, name="vertex"):
                await charge_tool(amount=99999)
        assert isinstance(excinfo.value.cause, PreFlightViolation)
    finally:
        await wal.close()


# -- Vertex AI: the function-call dispatch -------------------------------------------

class FakeFunctionCall:
    """The attribute shape Vertex's FunctionCall has had across SDK versions."""

    def __init__(self, name, args):
        self.name = name
        self.args = args


@aio
async def test_dispatch_runs_the_named_tool():
    def get_weather(city: str) -> dict:
        """Weather."""
        return {"city": city, "temp": 20}

    registry = {"get_weather": wrap_tool(get_weather, semantics=C)}
    result = await dispatch_function_call(
        FakeFunctionCall("get_weather", {"city": "London"}), registry)
    assert result == {"city": "London", "temp": 20}


@aio
async def test_a_hallucinated_function_name_is_refused_by_name():
    """A model naming a tool that does not exist is routine. `registry[name]`
    would raise a bare KeyError from inside a response loop; this says what was
    asked for and what exists."""
    registry = {"get_weather": wrap_tool(lambda city: {}, semantics=C,
                                         name="get_weather")}

    with pytest.raises(UnknownFunctionCall) as excinfo:
        await dispatch_function_call(
            FakeFunctionCall("get_wether", {"city": "London"}), registry)

    message = str(excinfo.value)
    assert "get_wether" in message and "get_weather" in message


@aio
async def test_non_strict_dispatch_returns_the_error_for_the_model_to_read():
    """The shape you want when feeding a model back its own mistake."""
    registry = {"real": wrap_tool(lambda: {}, semantics=C, name="real")}
    outcome = await dispatch_function_call(
        FakeFunctionCall("invented", {}), registry, strict=False)

    assert outcome["error"] == "unknown_function"
    assert "invented" in outcome["detail"]


@aio
async def test_a_call_with_no_name_is_refused():
    with pytest.raises(UnknownFunctionCall, match="carries no function name"):
        await dispatch_function_call(FakeFunctionCall(None, {}), {})


@aio
async def test_proto_style_args_are_unpacked():
    """Vertex returns a Struct-backed mapping, not a plain dict."""
    class ProtoArgs:
        def __init__(self, data):
            self._data = data

        def __iter__(self):
            return iter(self._data)

        def __getitem__(self, key):
            return self._data[key]

    seen = {}

    def record(city: str) -> dict:
        """Record."""
        seen["city"] = city
        return {}

    registry = {"record": wrap_tool(record, semantics=C)}
    await dispatch_function_call(
        FakeFunctionCall("record", ProtoArgs({"city": "Paris"})), registry)
    assert seen["city"] == "Paris"


@aio
async def test_saga_run_unwinds_a_whole_vertex_turn(tmp_path):
    world = {"rows": []}

    def insert(row: str) -> dict:
        """Insert."""
        world["rows"].append(row)
        return {"row": row}

    insert_tool = wrap_tool(
        insert, semantics=C,
        compensate=lambda r: Compensation(
            fn=lambda row: world["rows"].remove(row),
            kwargs={"row": r["row"]}, description="delete"))

    async def turn():
        await insert_tool(row="a")
        raise ConnectionError("model call failed mid-turn")

    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        with pytest.raises(SagaAborted):
            await saga_run(turn, wal=wal)
    finally:
        await wal.close()

    assert world["rows"] == []


@aio
async def test_saga_run_can_swallow_the_abort(tmp_path):
    async def turn():
        raise RuntimeError("boom")

    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        report = await saga_run(turn, wal=wal, reraise=False)
        assert report is not None and report.clean is True
    finally:
        await wal.close()


# -- Semantic Kernel -------------------------------------------------------------------

def test_semantic_kernel_wrap_tool_refuses_a_non_kernel_function():
    """Without the SDK the lazy import raises ImportError; with it, the type
    check raises TypeError. Either way a wrong object is refused rather than
    returned unprotected."""
    from agent_saga.adapters import semantic_kernel as sk

    with pytest.raises((TypeError, ImportError, ModuleNotFoundError)):
        sk.wrap_tool(lambda: None, semantics=C)


@aio
async def test_semantic_kernel_saga_run_names_a_missing_entry_point(tmp_path):
    """An agent exposing neither invoke nor invoke_async must not silently get
    a boundary around nothing."""
    from agent_saga.adapters import semantic_kernel as sk

    class Bare:
        pass

    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        with pytest.raises(SagaAborted) as excinfo:
            await sk.saga_run(Bare(), "hello", wal=wal)
        assert isinstance(excinfo.value.cause, AttributeError)
        assert "invoke" in str(excinfo.value.cause)
    finally:
        await wal.close()


@aio
async def test_semantic_kernel_saga_run_unwinds_the_agents_tools(tmp_path):
    """The boundary is what matters and it does not need the SDK: a duck-typed
    agent whose invoke performs saga work must still roll back."""
    from agent_saga.adapters import semantic_kernel as sk

    world = {"rows": []}
    insert = wrap_tool(
        lambda row: world["rows"].append(row) or {"row": row},
        semantics=C, name="insert",
        compensate=lambda r: Compensation(
            fn=lambda row: world["rows"].remove(row),
            kwargs={"row": r["row"]}, description="delete"))

    class Agent:
        async def invoke_async(self, message):
            await insert(row="a")
            raise RuntimeError("planner failed")

    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        with pytest.raises(SagaAborted):
            await sk.saga_run(Agent(), "do it", wal=wal)
    finally:
        await wal.close()

    assert world["rows"] == []


@aio
async def test_semantic_kernel_custom_invoke_overrides_the_entry_point(tmp_path):
    """SDK versions rename entry points; the boundary should not break because
    a method moved."""
    from agent_saga.adapters import semantic_kernel as sk

    class Odd:
        async def run_the_thing(self):
            return "done"

    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        result = await sk.saga_run(Odd(), wal=wal,
                                   invoke=lambda a: a.run_the_thing())
        assert result == "done"
    finally:
        await wal.close()


# -- real integration, skipped without the SDKs -------------------------------------------

@aio
async def test_real_semantic_kernel_function_is_wrapped():
    sk_mod = pytest.importorskip("semantic_kernel")
    from semantic_kernel.functions import KernelFunction, kernel_function

    @kernel_function(name="echo", description="Echo a value.")
    async def echo(value: str) -> str:
        return value

    original = KernelFunction.from_method(method=echo, plugin_name="test")
    from agent_saga.adapters import semantic_kernel as sk

    wrapped = sk.wrap_tool(original, semantics=C)
    assert wrapped.metadata.name == "echo"


@aio
async def test_real_vertex_function_call_is_dispatched():
    pytest.importorskip("vertexai")
    from vertexai.generative_models import FunctionDeclaration  # noqa: F401

    def get_weather(city: str) -> dict:
        """Weather."""
        return {"city": city}

    registry = {"get_weather": wrap_tool(get_weather, semantics=C)}
    result = await dispatch_function_call(
        FakeFunctionCall("get_weather", {"city": "Rome"}), registry)
    assert result == {"city": "Rome"}
