"""Distributed cross-fleet entanglement propagation.

The saga correlation headers (X-Saga-Correlation-Id, X-Saga-Entanglement-Id,
X-Saga-Parent-Step) are what let a multi-agent fleet -- Agent A calls Agent B
calls Agent C -- share one distributed saga identity, so a failure anywhere can
cascade a rollback across all of them.

``EntanglementPropagator`` does the plumbing automatically:

  * OUTGOING -- on every inter-service HTTP call it injects the current saga's
    correlation headers, so the callee inherits the entanglement id.
  * INCOMING -- on every request it extracts those headers and binds the
    correlation id for the request, so work the callee does (and any further
    calls it makes) stays on the same distributed saga.

Framework glue is provided for HTTPX (outgoing), and FastAPI/Starlette and Flask
(incoming). None of those are imported unless you actually wire the adapter, so
the core package stays dependency-free.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Iterator, Optional

from .entanglement import (
    HEADER_CORRELATION_ID,
    HEADER_ENTANGLEMENT_ID,
    HEADER_PARENT_STEP,
    get_correlation_headers,
    get_entanglement_matrix,
)

logger = logging.getLogger("agent_saga.propagation")

_SAGA_HEADERS = (HEADER_CORRELATION_ID, HEADER_ENTANGLEMENT_ID, HEADER_PARENT_STEP)


def _safe_header_value(value: Any) -> str:
    """Strip CR/LF (and control chars) so a saga name/id that contains a newline
    cannot inject extra headers when this value is written to an outgoing
    request. Defense in depth -- most HTTP stacks reject these, but the header is
    built from a user-supplied saga name, so we never trust it."""
    return "".join(ch for ch in str(value) if ch not in "\r\n" and ch >= " ")


class EntanglementPropagator:
    """Injects and extracts saga correlation headers across service boundaries."""

    def __init__(self, matrix: Any = None):
        # An explicit matrix, else the process-wide default. Used to stamp the
        # entanglement id when no saga context is active on the caller side.
        self._matrix = matrix

    @property
    def matrix(self) -> Any:
        return self._matrix or get_entanglement_matrix()

    # -- core: build outgoing headers, read incoming ones ------------------

    def outgoing_headers(self, existing: Optional[dict] = None) -> dict:
        """The saga headers to attach to an outgoing call, derived from the active
        saga context (or the current correlation id, or the matrix id as a last
        resort). Merged onto `existing` so caller headers are preserved."""
        headers = dict(existing or {})
        ctx = self._current_saga()
        if ctx is not None:
            for k, v in get_correlation_headers(ctx).items():
                headers[k] = _safe_header_value(v)
            return headers

        sid = self._current_correlation_id()
        if sid:
            headers[HEADER_CORRELATION_ID] = _safe_header_value(sid)
            headers.setdefault(HEADER_ENTANGLEMENT_ID, _safe_header_value(sid))
        else:
            headers.setdefault(HEADER_ENTANGLEMENT_ID, _safe_header_value(self.matrix.matrix_id))
        return headers

    def extract(self, headers: Any) -> dict:
        """Pull the saga ids from an incoming request's headers (case-insensitive),
        returning {correlation_id, entanglement_id, parent_step} with None for any
        that are absent."""
        def get(name: str) -> Optional[str]:
            if headers is None:
                return None
            val = None
            getter = getattr(headers, "get", None)
            if callable(getter):
                val = headers.get(name) or headers.get(name.lower())
            return val

        return {
            "correlation_id": get(HEADER_CORRELATION_ID),
            "entanglement_id": get(HEADER_ENTANGLEMENT_ID),
            "parent_step": get(HEADER_PARENT_STEP),
        }

    @contextlib.contextmanager
    def bind_incoming(self, headers: Any) -> Iterator[dict]:
        """Bind the incoming correlation id for the duration of a request, so the
        callee's logs, spans, and further outgoing calls stay on the same
        distributed saga. Restores the previous binding on exit."""
        info = self.extract(headers)
        sid = info["correlation_id"] or info["entanglement_id"]
        token = None
        if sid:
            from .observability import set_saga_id
            token = set_saga_id(sid)
        try:
            yield info
        finally:
            if token is not None:
                from .observability import reset_saga_id
                reset_saga_id(token)

    # -- HTTPX (outgoing) --------------------------------------------------

    def httpx_request_hook(self, request: Any) -> None:
        """An httpx `event_hooks={"request": [...]}` callback: stamps the saga
        headers onto every outgoing request."""
        try:
            for k, v in self.outgoing_headers().items():
                request.headers[k] = v
        except Exception:
            logger.debug("failed to inject entanglement headers on outgoing request",
                         exc_info=True)

    def apply_httpx(self, client: Any) -> Any:
        """Install the outgoing-header hook on an httpx Client/AsyncClient."""
        hooks = getattr(client, "event_hooks", None) or {}
        req_hooks = list(hooks.get("request", []))
        req_hooks.append(self.httpx_request_hook)
        hooks["request"] = req_hooks
        client.event_hooks = hooks
        return client

    # -- FastAPI / Starlette (incoming) ------------------------------------

    def starlette_middleware(self):
        """Return an ASGI middleware class that binds the incoming correlation id
        for each request. Use with ``app.add_middleware(propagator.starlette_middleware())``."""
        propagator = self

        class _EntanglementASGIMiddleware:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope.get("type") != "http":
                    await self.app(scope, receive, send)
                    return
                raw = scope.get("headers") or []
                headers = {k.decode("latin1"): v.decode("latin1") for k, v in raw}
                with propagator.bind_incoming(headers):
                    await self.app(scope, receive, send)

        return _EntanglementASGIMiddleware

    def install_fastapi(self, app: Any) -> None:
        """Register the incoming-binding middleware on a FastAPI/Starlette app."""
        app.add_middleware(self.starlette_middleware())

    # -- Flask (incoming) --------------------------------------------------

    def install_flask(self, app: Any) -> None:
        """Bind the incoming correlation id for the duration of each Flask request
        via before/after request hooks."""
        from flask import request, g

        @app.before_request
        def _bind():
            cm = self.bind_incoming(request.headers)
            g._saga_entanglement_cm = cm
            g._saga_entanglement_info = cm.__enter__()

        @app.teardown_request
        def _unbind(exc=None):
            cm = getattr(g, "_saga_entanglement_cm", None)
            if cm is not None:
                cm.__exit__(None, None, None)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _current_saga() -> Any:
        try:
            from .decorator import current_saga
            return current_saga()
        except Exception:
            return None

    @staticmethod
    def _current_correlation_id() -> Optional[str]:
        try:
            from .observability import current_correlation
            sid, _ = current_correlation()
            return sid
        except Exception:
            return None


__all__ = ["EntanglementPropagator"]
