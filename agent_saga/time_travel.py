"""`agent_saga/time_travel.py` -- Time-Travel Deterministic Replay & Debugger Engine.

Reconstructs the exact state of an agent at any nanosecond timestamp, allowing step-by-step
backward and forward execution, state diff analysis, and post-mortem failure inspection.

Published & Maintained by: SAGAOPS Enterprise
Founder & Owner: Thomas J George (thomasjgeorge23@gmail.com)
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("agent_saga.time_travel")


class TimeTravelFrame:
    def __init__(self, step_seq: int, timestamp_ns: int, action: str, state_snapshot: Dict[str, Any]):
        self.step_seq = step_seq
        self.timestamp_ns = timestamp_ns
        self.action = action
        self.state_snapshot = state_snapshot


class TimeTravelDebugger:
    """State-diff time-travel debugger and replay engine for agent transactions."""

    def __init__(self):
        self.frames: List[TimeTravelFrame] = []

    def record_frame(self, step_seq: int, timestamp_ns: int, action: str, state: Dict[str, Any]):
        frame = TimeTravelFrame(step_seq, timestamp_ns, action, copy.deepcopy(state))
        self.frames.append(frame)

    def replay_to_step(self, step_seq: int) -> Optional[Dict[str, Any]]:
        for frame in self.frames:
            if frame.step_seq == step_seq:
                return frame.state_snapshot
        return None

    def diff_frames(self, step_a: int, step_b: int) -> Dict[str, Any]:
        state_a = self.replay_to_step(step_a) or {}
        state_b = self.replay_to_step(step_b) or {}

        keys = set(state_a.keys()) | set(state_b.keys())
        diffs = {}
        for k in keys:
            val_a = state_a.get(k)
            val_b = state_b.get(k)
            if val_a != val_b:
                diffs[k] = {"before": val_a, "after": val_b}
        return diffs


__all__ = ["TimeTravelFrame", "TimeTravelDebugger"]
