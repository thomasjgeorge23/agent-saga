"""Durable Saga Memory & Multi-Day Transaction State Store.

Expands Write-Ahead Logs into persistent long-lived transaction stores that survive
days or weeks across chat sessions, allowing AI agents to query multi-day progress.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("agent_saga.durable_memory")


class DurableSagaMemory:
    """Multi-day transaction state store backed by durable JSONL / SQLite storage."""

    def __init__(self, memory_dir: str | Path):
        self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.db_file = self.memory_dir / "durable_sagas.jsonl"

    def persist_state(self, saga_id: str, status: str, state_data: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Save or update multi-day transaction state."""
        record = {
            "saga_id": saga_id,
            "status": status,
            "state_data": state_data,
            "metadata": metadata or {},
            "updated_at": time.time(),
        }

        with open(self.db_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

        logger.info("Persisted durable saga state for '%s' (status: %s)", saga_id, status)
        return record

    def load_state(self, saga_id: str) -> Optional[Dict[str, Any]]:
        """Load the latest persistent state for a multi-day transaction."""
        if not self.db_file.exists():
            return None

        latest_record = None
        with open(self.db_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                data = json.loads(line)
                if data.get("saga_id") == saga_id:
                    latest_record = data

        return latest_record

    def list_active_sagas(self) -> List[Dict[str, Any]]:
        """List all multi-day transactions currently active or pending completion."""
        if not self.db_file.exists():
            return []

        latest_by_id: Dict[str, Dict[str, Any]] = {}
        with open(self.db_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                data = json.loads(line)
                latest_by_id[data["saga_id"]] = data

        return [rec for rec in latest_by_id.values() if rec.get("status") in ("ACTIVE", "SUSPENDED", "IN_PROGRESS")]

    def get_saga_status_summary(self, saga_id: str) -> str:
        """Construct a human-readable AI summary string for a multi-day saga."""
        state = self.load_state(saga_id)
        if not state:
            return f"No transaction record found for saga '{saga_id}'."

        status = state.get("status", "UNKNOWN")
        updated_at = time.ctime(state.get("updated_at", time.time()))
        meta = state.get("metadata", {})
        item = meta.get("item", "order")

        return (
            f"Multi-day transaction '{saga_id}' ({item}) is {status}. "
            f"Last updated on {updated_at}. Summary: {state.get('state_data', {})}"
        )
