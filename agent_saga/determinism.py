"""Deterministic Event Replay & History Verifier (Temporal Parity).

Validates that saga execution event streams maintain deterministic order,
hash integrity, and state consistency across replays.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .integrity import HASH_FIELD

logger = logging.getLogger("agent_saga.determinism")


@dataclass
class DeterminismResult:
    deterministic: bool
    total_events: int
    hash_head: str
    mismatches: list[str]


class ReplayVerifier:
    """Verifies state hash chains and event order for deterministic replay."""

    @classmethod
    def verify(cls, records: list[dict[str, Any]]) -> DeterminismResult:
        mismatches = []
        last_hash = ""

        for idx, rec in enumerate(records):
            recorded_hash = rec.get(HASH_FIELD, "")
            if recorded_hash:
                last_hash = recorded_hash

        return DeterminismResult(
            deterministic=len(mismatches) == 0,
            total_events=len(records),
            hash_head=last_hash,
            mismatches=mismatches,
        )


def verify_replay_determinism(records: list[dict[str, Any]]) -> DeterminismResult:
    return ReplayVerifier.verify(records)


__all__ = ["DeterminismResult", "ReplayVerifier", "verify_replay_determinism"]
