"""Native OpenTelemetry Protocol (OTLP) Exporter.

Exports saga traces, compensation spans, gate verdicts, and circuit breaker
events directly via standard OTLP JSON over HTTP or gRPC endpoints (Datadog,
Jaeger, SigNoz, Honeycomb, Dynatrace).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from typing import Any, Optional

logger = logging.getLogger("agent_saga.observability.otlp")


class OTLPExporter:
    """Exports OpenTelemetry spans in standard OTLP/HTTP JSON format.

    Spans are batched, not sent one call at a time. Two triggers flush the
    buffer, so a high-throughput saga fleet neither floods the OTLP endpoint nor
    lets spans go stale:

    * ``batch_size``       -- flush as soon as this many spans accumulate, and
      cap each HTTP request to this many spans so a backlog is chunked rather
      than posted as one oversized payload the collector may reject.
    * ``flush_interval_ms`` -- when > 0, a background daemon thread flushes on
      this cadence so low-traffic spans still ship promptly. Start it with
      ``start()`` / ``with exporter:`` and stop with ``stop()``.

    On a failed POST the unsent spans are put back on the buffer for the next
    flush, so a transient collector outage does not drop telemetry.
    """

    def __init__(
        self,
        endpoint: str = "http://localhost:4318/v1/traces",
        headers: Optional[dict[str, str]] = None,
        service_name: str = "agent-saga",
        *,
        batch_size: int = 100,
        flush_interval_ms: float = 0.0,
    ):
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.endpoint = endpoint
        self.headers = headers or {"Content-Type": "application/json"}
        self.service_name = service_name
        self.batch_size = batch_size
        self.flush_interval_ms = flush_interval_ms
        self.spans: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._timer: Optional[threading.Thread] = None

    def create_span(
        self,
        name: str,
        saga_id: str,
        attributes: Optional[dict[str, Any]] = None,
        duration_ms: float = 0.0,
    ) -> dict[str, Any]:
        now_ns = int(time.time() * 1e9)
        attr_list = [
            {"key": "saga.id", "value": {"stringValue": saga_id}},
            {"key": "service.name", "value": {"stringValue": self.service_name}},
        ]
        if attributes:
            for k, v in attributes.items():
                attr_list.append({"key": k, "value": {"stringValue": str(v)}})

        clean_saga = saga_id.replace("-", "").zfill(32)[:32]
        span = {
            "traceId": clean_saga,
            "spanId": os.urandom(8).hex(),
            "name": name,
            "startTimeUnixNano": str(now_ns),
            "endTimeUnixNano": str(now_ns + int(duration_ms * 1e6)),
            "attributes": attr_list,
        }
        with self._lock:
            self.spans.append(span)
            full = len(self.spans) >= self.batch_size
        if full:
            self.export()          # size-triggered flush
        return span

    def _envelope(self, spans: list[dict[str, Any]]) -> bytes:
        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": self.service_name}}
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "agent_saga.observability"},
                            "spans": spans,
                        }
                    ],
                }
            ]
        }
        return json.dumps(payload).encode("utf-8")

    def _post(self, spans: list[dict[str, Any]]) -> bool:
        try:
            req = urllib.request.Request(self.endpoint, data=self._envelope(spans),
                                         headers=self.headers, method="POST")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return 200 <= resp.status < 300
        except Exception as exc:
            logger.debug("OTLP export to %s failed (offline or mock): %r", self.endpoint, exc)
            return False

    def export(self) -> bool:
        """Flush the buffer in batch_size chunks. Unsent spans are re-buffered on
        failure so a transient outage does not drop telemetry."""
        with self._lock:
            pending, self.spans = self.spans, []
        if not pending:
            return True
        for i in range(0, len(pending), self.batch_size):
            chunk = pending[i:i + self.batch_size]
            if not self._post(chunk):
                # Put the unsent remainder back, preserving order, and stop.
                with self._lock:
                    self.spans = pending[i:] + self.spans
                return False
        return True

    # -- background flush timer -------------------------------------------

    def start(self) -> "OTLPExporter":
        """Begin periodic flushing (no-op if flush_interval_ms == 0). Idempotent."""
        if self.flush_interval_ms <= 0 or (self._timer and self._timer.is_alive()):
            return self
        self._stop.clear()
        self._timer = threading.Thread(target=self._run_timer, name="otlp-flush", daemon=True)
        self._timer.start()
        return self

    def stop(self) -> None:
        """Stop the flush timer and flush whatever remains."""
        self._stop.set()
        if self._timer:
            self._timer.join(timeout=2.0)
            self._timer = None
        self.export()

    def _run_timer(self) -> None:
        interval = self.flush_interval_ms / 1000.0
        while not self._stop.wait(interval):
            try:
                self.export()
            except Exception:
                logger.exception("OTLP background flush failed; continuing")

    def __enter__(self) -> "OTLPExporter":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


__all__ = ["OTLPExporter"]
