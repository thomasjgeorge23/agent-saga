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
    """Thread-safe, enterprise state store for the control plane server."""

    def __init__(self):
        self._lock = threading.Lock()
        self.wal_records: list[dict[str, Any]] = []
        self.approvals: dict[str, dict[str, Any]] = {}
        self.entanglement_nodes: dict[str, dict[str, Any]] = {}
        self.entanglement_edges: list[dict[str, Any]] = []
        self.budget_log: list[dict[str, Any]] = []

    def ingest_wal(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            self.wal_records.extend(records)
            count = len(records)
        return {"status": "accepted", "records_ingested": count}

    def sync_entanglement(self, summary: dict[str, Any]) -> dict[str, Any]:
        agent_id = summary.get("agent_id") or f"agent-{len(self.entanglement_nodes)+1}"
        with self._lock:
            self.entanglement_nodes[agent_id] = summary
            count = len(self.entanglement_nodes)
        return {"status": "synced", "nodes": count}

    def get_fleet_budget(self) -> dict[str, Any]:
        with self._lock:
            total = sum(r.get("used", 0) for r in self.budget_log)
            node_count = len(self.entanglement_nodes)
        return {"status": "ok", "total_spend": total, "fleet_nodes": node_count}

    def get_fleet_entanglement(self) -> dict[str, Any]:
        with self._lock:
            nodes = [{"id": k, **v} for k, v in self.entanglement_nodes.items()]
            edges = list(self.entanglement_edges)
        return {"nodes": nodes, "edges": edges}

    def add_approval(self, req: dict[str, Any]) -> dict[str, Any]:
        req_id = req.get("id") or f"appr-{len(self.approvals)+1}"
        with self._lock:
            self.approvals[req_id] = {**req, "id": req_id, "status": req.get("status", "PENDING")}
            return self.approvals[req_id]

    def decide_approval(self, req_id: str, granted: bool, approver: str = "cloud_admin") -> Optional[dict[str, Any]]:
        with self._lock:
            appr = self.approvals.get(req_id)
            if not appr:
                return None
            appr["status"] = "GRANTED" if granted else "DENIED"
            appr["approver"] = approver
            appr["decided_at"] = time.time()
            return appr

    def get_approvals(self, status: str = "pending") -> list[dict[str, Any]]:
        with self._lock:
            if status == "all":
                return list(self.approvals.values())
            target = status.upper()
            return [a for a in self.approvals.values() if a.get("status") == target]

    def get_audit_report(self, start: Any = None, end: Any = None, format: str = "csv") -> dict[str, Any]:
        with self._lock:
            records = list(self.wal_records)
        if format == "csv":
            lines = ["seq,ts,event,saga_id,step_id,tool,status"]
            for r in records[:500]:
                lines.append(f"{r.get('seq','')},{r.get('ts','')},{r.get('event','')},{r.get('saga_id','')},{r.get('step_id','')},{r.get('tool','')},{r.get('status','')}")
            content = "\n".join(lines)
        else:
            content = json.dumps(records, indent=2, default=str)

        return {
            "status": "generated",
            "format": format,
            "record_count": len(records),
            "generated_at": time.time(),
            "content": content,
            "download_url": f"/v1/audit/download?format={format}",
        }


_GLOBAL_STATE = SagaCloudServerState()


class _CloudServerHandler(BaseHTTPRequestHandler):

    auth_token: Optional[str] = None

    def log_message(self, format, *args):
        pass  # silent logging

    def _check_auth(self) -> bool:
        if not self.auth_token:
            return True
        header = self.headers.get("Authorization", "")
        expected = f"Bearer {self.auth_token}"
        return header == expected

    def _json(self, status_code: int, data: dict[str, Any]) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._check_auth():
            return self._json(401, {"error": "unauthorized"})
        path = self.path.split("?")[0]
        if path == "/v1/approvals":
            self._json(200, {"approvals": _GLOBAL_STATE.get_approvals("pending")})
        elif path == "/v1/fleet/budget":
            self._json(200, _GLOBAL_STATE.get_fleet_budget())
        elif path == "/v1/fleet/entanglement":
            self._json(200, _GLOBAL_STATE.get_fleet_entanglement())
        elif path == "/v1/audit/report":
            self._json(200, _GLOBAL_STATE.get_audit_report())
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self):
        if not self._check_auth():
            return self._json(401, {"error": "unauthorized"})
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
        elif path == "/v1/approvals/decide":
            req_id = payload.get("id")
            granted = bool(payload.get("granted", True))
            approver = payload.get("approver", "cloud_admin")
            res = _GLOBAL_STATE.decide_approval(req_id, granted=granted, approver=approver)
            if res:
                self._json(200, {"status": "decided", "approval": res})
            else:
                self._json(404, {"error": "approval_not_found"})
        else:
            self._json(404, {"error": "not_found"})


class SagaCloudServer:
    """Self-hosted control plane HTTPServer for testing and local fleet ops."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8090, auth_token: Optional[str] = None):
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self.server: Optional[HTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        handler_cls = _CloudServerHandler
        handler_cls.auth_token = self.auth_token
        self.server = HTTPServer((self.host, self.port), handler_cls)
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
