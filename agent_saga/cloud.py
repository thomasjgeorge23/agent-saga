"""agent-saga Cloud Managed SaaS Control Plane Exporter & Telemetry Client.

Enables managed cloud auditing, Slack/Teams approval gateways, cross-fleet
entanglement tracking, and compliance reporting via a sagaops.dev-compatible
SaaS API.

The client speaks plain HTTPS with nothing beyond the standard library, matching
the rest of the package's zero-dependency discipline. Blocking socket I/O is run
off the event loop, and -- because telemetry must never take a saga down -- a
transport failure degrades to a structured error dict by default rather than
raising. Pass ``raise_on_error=True`` if you would rather a failed push surface.

Tests (and fully offline deployments) can inject a ``transport`` callable to
substitute the network entirely.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

logger = logging.getLogger("agent_saga.cloud")

# A transport is anything that can turn a request into a response dict. It gets
# (url, payload, headers) and returns the parsed JSON body. Used for tests and
# for swapping in a different HTTP stack (e.g. requests/httpx) if one is present.
Transport = Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]


class SagaCloudClient:
    """Telemetry exporter and managed approval gateway client for sagaops.dev."""

    def __init__(
        self,
        api_key: str,
        endpoint: str = "https://api.sagaops.dev/v1",
        *,
        timeout: float = 10.0,
        transport: Optional[Transport] = None,
        raise_on_error: bool = False,
        dry_run: bool = False,
    ):
        if not api_key:
            raise ValueError("SagaCloudClient requires an api_key")
        self.api_key = api_key
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self._transport = transport
        self.raise_on_error = raise_on_error
        self.dry_run = dry_run
        """When True, every call logs exactly what it *would* send and returns a
        synthetic ``{"status": "dry_run", ...}`` response without touching the
        network. The safe way to verify what will leave the building before a team
        enables live cloud sync in production."""

    # -- public API --------------------------------------------------------

    async def push_wal_records(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """Push WAL execution records to the hosted audit log & compliance
        dashboard. Returns the server's response, or an error dict on failure."""
        result = await self._post("/wal/ingest", {"records": records})
        if result.get("status") == "accepted" and "records_ingested" not in result:
            # Be forgiving of a server that only echoes ``accepted``.
            result["records_ingested"] = len(records)
        logger.info(
            "Pushed %d WAL record(s) to agent-saga Cloud (%s): %s",
            len(records), self.endpoint, result.get("status"),
        )
        return result

    async def sync_entanglement(self, summary: dict[str, Any]) -> dict[str, Any]:
        """Sync the cross-fleet multi-agent entanglement matrix graph."""
        result = await self._post("/entanglement/sync", {"summary": summary})
        if result.get("status") == "synced" and "nodes" not in result:
            result["nodes"] = summary.get("active_nodes", 0)
        return result

    # -- transport ---------------------------------------------------------

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.endpoint}{path}"
        if self.dry_run:
            logger.info(
                "agent-saga Cloud DRY-RUN: would POST to %s with %d top-level "
                "field(s): %s", url, len(payload), sorted(payload.keys()))
            return {"status": "dry_run", "endpoint": url,
                    "would_send": payload, "sent": False}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "agent-saga-cloud/1",
        }
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._send, url, payload, headers)
        except Exception as exc:  # network, timeout, HTTP error, bad JSON
            if self.raise_on_error:
                raise
            logger.warning("agent-saga Cloud push to %s failed: %r", url, exc)
            return {"status": "error", "error": str(exc), "endpoint": url}

    def _send(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        """Blocking POST. Runs on a worker thread via the executor."""
        if self._transport is not None:
            return self._transport(url, payload, headers)

        data = json.dumps(payload, default=str).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body) if body else {"status": "accepted"}


__all__ = ["SagaCloudClient", "Transport"]
