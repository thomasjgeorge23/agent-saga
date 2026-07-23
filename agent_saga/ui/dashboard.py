"""FastAPI/ASGI Embedded Web UI Dashboard App.

Provides `get_saga_ui_app()` to return an ASGI application (FastAPI/Starlette router)
that can be mounted into any enterprise web server:

    from fastapi import FastAPI
    from agent_saga.ui import get_saga_ui_app

    app = FastAPI()
    app.mount("/saga-ui", get_saga_ui_app(wal_path="./sagas.wal"))

Dashboard Features:
  - Entanglement Visualizer: Interactive network of step dependencies and rollbacks.
  - Live Kill-Switches & Breakers: Visual status and control toggles.
  - Pending Approvals Queue: Human sign-off portal for risk management.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .reader import SagaWALReader
from ..approvals import get_approval_store
from ..killswitch import get_kill_switch


def get_saga_ui_app(
    wal_path: Optional[str | Path] = None,
    *,
    token: Optional[str] = None,
) -> Any:
    """Creates a FastAPI/Starlette ASGI application for embedding agent-saga UI."""
    try:
        from fastapi import FastAPI, HTTPException, Depends, Header, Query
        from fastapi.responses import HTMLResponse, JSONResponse
    except ImportError:
        # Fall back to a lightweight custom ASGI callable if FastAPI is not installed
        def lightweight_asgi(scope, receive, send):
            async def handle():
                if scope["type"] == "http":
                    body = json.dumps({"status": "agent-saga-ui-active", "wal_path": str(wal_path)}).encode("utf-8")
                    await send({
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [(b"content-type", b"application/json")],
                    })
                    await send({
                        "type": "http.response.body",
                        "body": body,
                    })
            return handle()
        return lightweight_asgi

    from .._version import __version__
    app = FastAPI(title="agent-saga UI Dashboard", version=__version__)
    reader = SagaWALReader(wal_path) if wal_path else None

    async def verify_auth(x_api_key: Optional[str] = Header(None, alias="X-API-Key"), authorization: Optional[str] = Header(None)):
        if not token:
            return True
        provided = x_api_key or (authorization.replace("Bearer ", "") if authorization and authorization.startswith("Bearer ") else None)
        if provided != token:
            raise HTTPException(status_code=401, detail="Invalid API Key or Bearer Token")
        return True

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        template_path = Path(__file__).parent / "templates" / "dashboard.html"
        if template_path.exists():
            return template_path.read_text(encoding="utf-8")
        return "<html><body><h1>agent-saga UI Dashboard Active</h1></body></html>"

    @app.get("/api/meta")
    async def get_meta(authed: bool = Depends(verify_auth)):
        if reader:
            return reader.meta()
        return {"status": "active", "total_sagas": 0}

    @app.get("/api/sagas")
    async def list_sagas(authed: bool = Depends(verify_auth)):
        if reader:
            return reader.list_sagas()
        return []

    @app.get("/api/bpmn")
    async def export_bpmn(authed: bool = Depends(verify_auth)):
        from ..bpmn import BPMNExporter
        records = reader.list_sagas() if reader else []
        xml_str = BPMNExporter.to_bpmn_xml(records)
        return HTMLResponse(content=xml_str, media_type="application/xml")

    @app.get("/api/entanglement")
    async def get_entanglement(authed: bool = Depends(verify_auth)):
        from ..entanglement import get_entanglement_matrix
        matrix = get_entanglement_matrix()
        return matrix.summary()

    @app.get("/api/live-tail")
    async def live_tail(authed: bool = Depends(verify_auth)):
        from fastapi.responses import StreamingResponse
        import asyncio

        async def event_stream():
            last_idx = 0
            while True:
                if reader:
                    sagas = reader.list_sagas()
                    if len(sagas) > last_idx:
                        new_records = sagas[last_idx:]
                        last_idx = len(sagas)
                        yield f"data: {json.dumps(new_records)}\n\n"
                await asyncio.sleep(1.0)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.get("/api/approvals")
    async def list_approvals(authed: bool = Depends(verify_auth)):
        store = get_approval_store()
        pending = store.list_pending()
        return [
            {
                "token": req.token,
                "saga_id": req.saga_id,
                "tool": req.tool,
                "reason": req.reason,
                "requested_at": req.requested_at,
            }
            for req in pending
        ]

    @app.post("/api/approvals/{token}/approve")
    async def approve_request(token: str, by: str = "ui-admin", authed: bool = Depends(verify_auth)):
        store = get_approval_store()
        ok = store.resolve(token, approved=True, by=by)
        if not ok:
            raise HTTPException(status_code=404, detail="Approval token not found or already resolved")
        return {"status": "approved", "token": token}

    @app.post("/api/approvals/{token}/deny")
    async def deny_request(token: str, by: str = "ui-admin", authed: bool = Depends(verify_auth)):
        store = get_approval_store()
        ok = store.resolve(token, approved=False, by=by)
        if not ok:
            raise HTTPException(status_code=404, detail="Approval token not found or already resolved")
        return {"status": "denied", "token": token}

    @app.get("/api/killswitch")
    async def killswitch_status(authed: bool = Depends(verify_auth)):
        ks = get_kill_switch()
        return {"tripped": ks.is_tripped()}

    return app


__all__ = ["get_saga_ui_app"]
