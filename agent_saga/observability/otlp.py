"""Native OpenTelemetry Protocol (OTLP) Exporter.

Exports saga traces, compensation spans, gate verdicts, and circuit breaker
events directly via standard OTLP JSON over HTTP or gRPC endpoints (Datadog,
Jaeger, SigNoz, Honeycomb, Dynatrace).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from typing import Any, Optional

logger = logging.getLogger("agent_saga.observability.otlp")


class OTLPExporter:
    """Exports OpenTelemetry spans in standard OTLP/HTTP JSON format."""

    def __init__(
        self,
        endpoint: str = "http://localhost:4318/v1/traces",
        headers: Optional[dict[str, str]] = None,
        service_name: str = "agent-saga",
    ):
        self.endpoint = endpoint
        self.headers = headers or {"Content-Type": "application/json"}
        self.service_name = service_name
        self.spans: list[dict[str, Any]] = []

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
        self.spans.append(span)
        return span

    def export(self) -> bool:
        if not self.spans:
            return True
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
                            "spans": list(self.spans),
                        }
                    ],
                }
            ]
        }
        data = json.dumps(payload).encode("utf-8")
        try:
            req = urllib.request.Request(self.endpoint, data=data, headers=self.headers, method="POST")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                success = 200 <= resp.status < 300
                if success:
                    self.spans.clear()
                return success
        except Exception as exc:
            logger.debug("OTLP export to %s failed (offline or mock): %r", self.endpoint, exc)
            return False


__all__ = ["OTLPExporter"]
