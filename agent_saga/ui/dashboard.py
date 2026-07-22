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

    app = FastAPI(title="agent-saga UI Dashboard", version="0.1.9")
    reader = SagaWALReader(wal_path) if wal_path else None

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        template_path = Path(__file__).parent / "templates" / "dashboard.html"
        if template_path.exists():
            return template_path.read_text(encoding="utf-8")
        return "<html><body><h1>agent-saga UI Dashboard Active</h1></body></html>"

    @app.get("/api/meta")
    async def get_meta():
        if reader:
            return reader.meta()
        return {"status": "active", "total_sagas": 0}

    @app.get("/api/sagas")
    async def list_sagas():
        if reader:
            return reader.list_sagas()
        return []

    @app.get("/api/approvals")
    async def list_approvals():
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
    async def approve_request(token: str, by: str = "ui-admin"):
        store = get_approval_store()
        ok = store.resolve(token, approved=True, by=by)
        if not ok:
            raise HTTPException(status_code=404, detail="Approval token not found or already resolved")
        return {"status": "approved", "token": token}

    @app.post("/api/approvals/{token}/deny")
    async def deny_request(token: str, by: str = "ui-admin"):
        store = get_approval_store()
        ok = store.resolve(token, approved=False, by=by)
        if not ok:
            raise HTTPException(status_code=404, detail="Approval token not found or already resolved")
        return {"status": "denied", "token": token}

    @app.get("/api/killswitch")
    async def killswitch_status():
        ks = get_kill_switch()
        return {"tripped": ks.is_tripped()}

    return app


__all__ = ["get_saga_ui_app"]
