"""Distributed cross-fleet entanglement propagation (#33): inject on outgoing
calls, extract+bind on incoming, so A->B->C share one distributed saga."""

from conftest import aio

from agent_saga import EntanglementPropagator, saga_scope
from agent_saga.entanglement import (
    HEADER_CORRELATION_ID, HEADER_ENTANGLEMENT_ID, HEADER_PARENT_STEP)
from agent_saga.observability import current_correlation, set_saga_id, reset_saga_id
from agent_saga.wal.file_wal import FileWAL


@aio
async def test_outgoing_headers_from_active_saga():
    p = EntanglementPropagator()
    wal = FileWAL(); await wal.start()
    async with saga_scope(name="agentA", wal=wal):
        h = p.outgoing_headers()
    assert h[HEADER_CORRELATION_ID].startswith("agentA")
    assert HEADER_ENTANGLEMENT_ID in h and HEADER_PARENT_STEP in h
    await wal.close()


def test_extract_is_case_insensitive():
    p = EntanglementPropagator()
    info = p.extract({HEADER_CORRELATION_ID.lower(): "s-1", HEADER_ENTANGLEMENT_ID: "m-1"})
    assert info["correlation_id"] == "s-1" and info["entanglement_id"] == "m-1"


def test_bind_incoming_binds_and_restores():
    p = EntanglementPropagator()
    with p.bind_incoming({HEADER_CORRELATION_ID: "s-99"}):
        sid, _ = current_correlation()
        assert sid == "s-99"
    assert current_correlation()[0] is None


def test_httpx_hook_injects_current_saga_id():
    p = EntanglementPropagator()

    class FakeReq:
        def __init__(self): self.headers = {}

    tok = set_saga_id("svc-777")
    try:
        req = FakeReq()
        p.httpx_request_hook(req)
    finally:
        reset_saga_id(tok)
    assert req.headers[HEADER_CORRELATION_ID] == "svc-777"


def test_apply_httpx_installs_hook():
    p = EntanglementPropagator()
    class FakeClient: pass
    c = FakeClient()
    p.apply_httpx(c)
    assert p.httpx_request_hook in c.event_hooks["request"]


@aio
async def test_asgi_middleware_propagates_a_to_b_to_c():
    p = EntanglementPropagator()
    seen = {}

    async def app_b(scope, receive, send):
        seen["b_sees"] = current_correlation()[0]
        # B, acting as a client to C, builds outgoing headers from its context
        seen["b_forwards"] = p.outgoing_headers().get(HEADER_CORRELATION_ID)
        await send({"type": "http.response.start", "status": 200})

    Middleware = p.starlette_middleware()
    scope = {"type": "http", "headers": [(HEADER_CORRELATION_ID.encode(), b"agentA-1")]}
    import asyncio
    await Middleware(app_b)(scope, None, lambda msg: asyncio.sleep(0))
    assert seen["b_sees"] == "agentA-1"          # B inherits A's saga
    assert seen["b_forwards"] == "agentA-1"      # and forwards the same id to C


@aio
async def test_asgi_middleware_passes_through_non_http():
    p = EntanglementPropagator()
    called = {"v": False}
    async def raw(scope, r, s): called["v"] = True
    await p.starlette_middleware()(raw)({"type": "lifespan"}, None, None)
    assert called["v"]
