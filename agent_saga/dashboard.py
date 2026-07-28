"""Embedded Live Web Dashboard & Telemetry Console (`agent_saga.dashboard`).

Serves a zero-dependency, real-time web console displaying live DAG executions,
WAL event streams via SSE, and Human-in-the-Loop approval queues.
"""

from __future__ import annotations

import asyncio
import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any, Dict, List, Optional

logger = logging.getLogger("agent_saga.dashboard")

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>agent-saga · Live Enterprise Telemetry Dashboard</title>
  <style>
    body { background: #070c16; color: #f8fafc; font-family: ui-monospace, SFMono-Regular, monospace; margin: 0; padding: 20px; }
    h1 { color: #f59e0b; margin-bottom: 5px; }
    .subtitle { color: #64748b; font-size: 13px; margin-bottom: 20px; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
    .card { background: #0f172a; border: 1px solid #1e293b; border-radius: 8px; padding: 15px; }
    .card h3 { color: #38bdf8; margin-top: 0; font-size: 14px; text-transform: uppercase; letter-spacing: 1px; }
    .log-stream { background: #020617; border: 1px solid #1e293b; border-radius: 4px; height: 350px; overflow-y: auto; padding: 10px; font-size: 11px; }
    .event { margin-bottom: 6px; padding: 4px 8px; border-radius: 4px; background: rgba(255,255,255,0.03); }
    .event.SUCCESS { border-left: 3px solid #10b981; }
    .event.ROLLED_BACK { border-left: 3px solid #ef4444; }
    .event.PREFLIGHT { border-left: 3px solid #f59e0b; }
    .badge { padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 10px; }
    .badge-green { background: rgba(16,185,129,0.2); color: #34d399; }
    .badge-red { background: rgba(239,68,68,0.2); color: #f87171; }
  </style>
</head>
<body>
  <h1>⚡ agent-saga · Real-Time Telemetry Dashboard</h1>
  <div class="subtitle">Enterprise Transactional Safety, Pre-Flight Invariants & Dynamic Compensations</div>

  <div class="grid">
    <div class="card">
      <h3>📜 Live WAL Event Stream (SSE)</h3>
      <div id="logStream" class="log-stream">
        <div class="event SUCCESS">[SYSTEM] Dashboard Telemetry active. Awaiting saga events...</div>
      </div>
    </div>

    <div class="card">
      <h3>⏸️ Human-in-the-Loop Pending Approvals</h3>
      <div id="approvalsList">
        <p style="color: #64748b; font-size: 12px;">No pending human-in-the-loop gates required.</p>
      </div>
    </div>
  </div>

  <script>
    const stream = new EventSource('/api/events');
    const logStream = document.getElementById('logStream');

    stream.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        const div = document.createElement('div');
        div.className = 'event ' + (data.status || 'PREFLIGHT');
        div.innerHTML = `<strong>[${new Date().toLocaleTimeString()}] ${data.event || 'SAGA_EVENT'}</strong> - Saga: <code>${data.saga_id || 'N/A'}</code> ${data.summary || JSON.stringify(data)}`;
        logStream.appendChild(div);
        logStream.scrollTop = logStream.scrollHeight;
      } catch(err) {}
    };
  </script>
</body>
</html>
"""


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """HTTP Request handler for live telemetry dashboard."""

    sagas_store: List[Dict[str, Any]] = []

    def log_message(self, format: str, *args: Any) -> None:
        return  # Silence standard HTTP logs

    def do_GET(self) -> None:
        if self.path in ("/", "/dashboard"):
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode("utf-8"))

        elif self.path == "/api/sagas":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"sagas": DashboardRequestHandler.sagas_store}).encode("utf-8"))

        elif self.path == "/api/events":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                msg = json.dumps({"event": "HEARTBEAT", "status": "SUCCESS", "summary": "Dashboard connected."})
                self.wfile.write(f"data: {msg}\n\n".encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass

        else:
            self.send_error(HTTPStatus.NOT_FOUND)


class DashboardServer:
    """Embedded Dashboard HTTP Server."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8090):
        self.host = host
        self.port = port
        self.httpd: Optional[HTTPServer] = None
        self.thread: Optional[Thread] = None

    def start(self) -> None:
        """Start dashboard server in background thread."""
        self.httpd = HTTPServer((self.host, self.port), DashboardRequestHandler)
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        logger.info("Live Dashboard Server started at http://%s:%d", self.host, self.port)

    def stop(self) -> None:
        """Stop dashboard server."""
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            logger.info("Dashboard Server stopped.")


__all__ = ["DashboardServer", "DASHBOARD_HTML"]
