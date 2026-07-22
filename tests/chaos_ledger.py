"""A durable external system, for chaos tests.

Stands in for Stripe. It lives in a file so it survives the death of the process
that wrote to it -- which is the whole point: after a SIGKILL, the only honest
question is what the *outside world* now looks like, and an in-memory fake
cannot answer it because it dies with the agent.

It records `attempts` separately from `applied`, and that distinction is the
one that makes the suite worth running. A refund issued twice against a real
payment processor is refused by the processor's own idempotency key, so a test
that only checked the final balance would pass whether the guarantee lives in
agent-saga or in Stripe. Recording both lets a test assert the stronger claim:
that the second call was never *made*, not merely that it was absorbed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    # -- writes ------------------------------------------------------------

    def _append(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
            fh.flush()
            os.fsync(fh.fileno())      # the outside world does not lose writes

    def charge(self, charge_id: str, amount: float) -> dict:
        self._append({"op": "charge", "id": charge_id, "amount": amount})
        return {"id": charge_id, "amount": amount}

    def refund(self, charge_id: str, idempotency_key: str = "") -> dict:
        """Every attempt is recorded; only the first with a given key applies.

        Modelled on how a payment processor actually behaves, so that a test
        can tell "we never called twice" from "we called twice and got lucky".
        """
        applied = not self._has_applied_refund(charge_id, idempotency_key)
        self._append({"op": "refund", "id": charge_id, "applied": applied,
                      "key": idempotency_key})
        return {"refunded": charge_id, "applied": applied}

    def _has_applied_refund(self, charge_id: str, key: str) -> bool:
        for record in self.records():
            if record.get("op") != "refund" or not record.get("applied"):
                continue
            if record.get("id") == charge_id:
                return True
            if key and record.get("key") == key:
                return True
        return False

    # -- reads -------------------------------------------------------------

    def records(self) -> list:
        try:
            with open(self.path, encoding="utf-8") as fh:
                out = []
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue        # torn final write
                return out
        except FileNotFoundError:
            return []

    def charges(self) -> list:
        return [r for r in self.records() if r.get("op") == "charge"]

    def refund_attempts(self, charge_id: str = "") -> list:
        return [r for r in self.records()
                if r.get("op") == "refund"
                and (not charge_id or r.get("id") == charge_id)]

    def refunds_applied(self, charge_id: str = "") -> list:
        return [r for r in self.refund_attempts(charge_id) if r.get("applied")]

    def outstanding(self) -> float:
        """Money the customer is still out of pocket. The number that matters."""
        total = 0.0
        for record in self.charges():
            if not self.refunds_applied(record["id"]):
                total += float(record.get("amount") or 0)
        return total

    def summary(self) -> str:
        return (f"{len(self.charges())} charge(s), "
                f"{len(self.refund_attempts())} refund attempt(s), "
                f"{len(self.refunds_applied())} applied, "
                f"outstanding={self.outstanding():g}")
