"""Self-Hosted Control Plane Server for agent-saga Cloud & sagaops.dev API.

Provides an embedded or standalone control plane server matching the
sagaops.dev API contract used by SagaCloudClient.
"""

from __future__ import annotations

import json
import logging
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Optional
import threading

logger = logging.getLogger("agent_saga.cloud_server")


class SagaCloudServerState:
    """In-memory store for the control plane server."""

    def __init__(self):
        self.wal_records: list[dict[str, Any]] = []
        self.approvals: list[dict[str, Any]] = []
        self.entanglement_nodes: dict[str, dict[str, Any]] = {}
        self.entanglement_edges: list[dict[str, Any]] = []
        self.budget_log: list[dict[str, Any]] = []

    def ingest_wal(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        self.wal_records.extend(records)
        return {"status": "accepted", "records_ingested": len(records)}

    def sync_entanglement(self, summary: dict[str, Any]) -> dict[str, Any]:
        agent_id = summary.get("agent_id") or f"agent-{len(self.entanglement_nodes)+1}"
        self.entanglement_nodes[agent_id] = summary
        return {"status": "synced", "nodes": len(self.entanglement_nodes)}

    def get_fleet_budget(self) -> dict[str, Any]:
        total = sum(r.get("used", 0) for r in self.budget_log)
        return {"status": "ok", "total_spend": total, "fleet_nodes": len(self.entanglement_nodes)}

    def get_fleet_entanglement(self) -> dict[str, Any]:
        return {
            "nodes": [{"id": k, **v} for k, v in self.entanglement_nodes.items()],
            "edges": self.entanglement_edges,
        }

    def get_audit_report(self, format: str = "csv") -> dict[str, Any]:
        return {
            "status": "generated",
            "format": format,
            "record_count": len(self.wal_records),
            "generated_at": time.time(),
            "download_url": "/audit/download",
        }


_GLOBAL_STATE = SagaCloudServerState()


class _CloudServerHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # silent logging

    def _json(self, status_code: int, data: dict[str, Any]) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/v1/approvals":
            self._json(200, {"approvals": _GLOBAL_STATE.approvals})
        elif path == "/v1/fleet/budget":
            self._json(200, _GLOBAL_STATE.get_fleet_budget())
        elif path == "/v1/fleet/entanglement":
            self._json(200, _GLOBAL_STATE.get_fleet_entanglement())
        elif path == "/v1/audit/report":
            self._json(200, _GLOBAL_STATE.get_audit_report())
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_len) if content_len > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = {}

        path = self.path.split("?")[0]
        if path == "/v1/wal/ingest":
            records = payload.get("records", [])
            res = _GLOBAL_STATE.ingest_wal(records)
            self._json(200, res)
        elif path == "/v1/entanglement/sync":
            summary = payload.get("summary", {})
            res = _GLOBAL_STATE.sync_entanglement(summary)
            self._json(200, res)
        else:
            self._json(404, {"error": "not_found"})


class SagaCloudServer:
    """Self-hosted control plane HTTPServer for testing and local fleet ops."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8090):
        self.host = host
        self.port = port
        self.server: Optional[HTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self.server = HTTPServer((self.host, self.port), _CloudServerHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        logger.info("SagaCloudServer listening on http://%s:%d", self.host, self.port)

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


def get_global_cloud_state() -> SagaCloudServerState:
    return _GLOBAL_STATE


__all__ = ["SagaCloudServer", "SagaCloudServerState", "get_global_cloud_state"]
