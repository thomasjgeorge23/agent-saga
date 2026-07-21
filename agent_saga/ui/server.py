"""Zero-dependency HTTP server for the time-travel debugger.

Built on the standard library's http.server rather than FastAPI, so the whole
thing runs with `python -m agent_saga.ui` and no `pip install` -- the property
you want from a tool you reach for during an incident. The routing is a thin
shell over `SagaWALReader`; swapping in FastAPI later means re-wiring three
routes, nothing more.

Binds to 127.0.0.1 by default. A WAL can contain business data (customer ids,
amounts, object ids), so this must not be exposed on 0.0.0.0 without a
deliberate choice -- the CLI warns when you make it.
"""

from __future__ import annotations

import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from .reader import SagaWALReader

logger = logging.getLogger("agent_saga.ui")

_TEMPLATE = Path(__file__).parent / "templates" / "dashboard.html"


def build_handler(reader: SagaWALReader, token: Optional[str] = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "agent-saga-ui"
        # HTTP/1.0: one request per connection. Over loopback the handshake is
        # free, and closing each connection means no handler thread lingers
        # holding a kept-alive socket -- simpler and leak-free for a local,
        # single-user tool. (Content-Length is still sent for correct framing.)
        protocol_version = "HTTP/1.0"

        # -- helpers -----------------------------------------------------

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # Read-only local tool; never let a browser or proxy cache stale runs.
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, payload) -> None:
            self._send(code, json.dumps(payload, default=str).encode("utf-8"),
                       "application/json; charset=utf-8")

        # -- routing -----------------------------------------------------

        def _authorized(self) -> bool:
            """Bearer token via `Authorization: Bearer <t>` or a `?token=` query
            param (so a browser can open one URL). Constant-time compared. When
            no token is configured, everything is allowed -- the local default."""
            if not token:
                return True
            provided = None
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                provided = auth[len("Bearer "):].strip()
            if provided is None:
                qs = parse_qs(urlparse(self.path).query)
                vals = qs.get("token")
                provided = vals[0] if vals else None
            return provided is not None and hmac.compare_digest(provided, token)

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            try:
                if not self._authorized():
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Bearer realm="agent-saga-ui"')
                    body = b'{"error": "unauthorized"}'
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(body)
                    return
                self._route()
            except BrokenPipeError:
                pass  # client navigated away mid-response; not our problem
            except Exception:
                logger.exception("request handling failed")
                try:
                    self._json(500, {"error": "internal error"})
                except Exception:
                    pass

        do_HEAD = do_GET

        def _route(self) -> None:
            path = urlparse(self.path).path

            if path == "/" or path == "/index.html":
                return self._serve_dashboard()
            if path == "/api/meta":
                return self._json(200, reader.meta())
            if path == "/api/sagas":
                return self._json(200, reader.list_sagas())
            if path.startswith("/api/sagas/"):
                saga_id = unquote(path[len("/api/sagas/"):]).strip("/")
                if not saga_id:
                    return self._json(400, {"error": "missing saga_id"})
                detail = reader.get_saga(saga_id)
                if detail is None:
                    return self._json(404, {"error": f"saga {saga_id!r} not found"})
                return self._json(200, detail)

            self._json(404, {"error": "not found"})

        def _serve_dashboard(self) -> None:
            try:
                html = _TEMPLATE.read_bytes()
            except FileNotFoundError:
                return self._json(500, {"error": "dashboard template missing"})
            self._send(200, html, "text/html; charset=utf-8")

        def log_message(self, fmt: str, *args) -> None:
            logger.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


def make_server(wal_path: str, host: str = "127.0.0.1", port: int = 8080,
                *, token: Optional[str] = None) -> ThreadingHTTPServer:
    reader = SagaWALReader(wal_path)
    return ThreadingHTTPServer((host, port), build_handler(reader, token))


def serve(wal_path: str, host: str = "127.0.0.1", port: int = 8080,
          *, token: Optional[str] = None) -> None:
    httpd = make_server(wal_path, host, port, token=token)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


__all__ = ["make_server", "serve", "build_handler"]
