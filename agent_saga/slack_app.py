"""Slack Block Kit Interactive App Integration for Risk Approvals.

Generates interactive Slack Block Kit messages with Approve / Deny buttons
and handles webhook callback payloads directly into the ApprovalStore.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .approvals import GRANTED, DENIED, get_approval_store, ApprovalRequest


class SlackBlockKitApp:
    """Generates Slack Block Kit payloads and processes interactive button callbacks."""

    @classmethod
    def build_approval_block(cls, req: ApprovalRequest) -> dict[str, Any]:
        return {
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": "⚡ agent-saga Approval Request Required"},
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Saga ID:*\n`{req.saga_id}`"},
                        {"type": "mrkdwn", "text": f"*Tool / Action:*\n`{req.tool}`"},
                        {"type": "mrkdwn", "text": f"*Rule Triggered:*\n`{req.rule}`"},
                        {"type": "mrkdwn", "text": f"*Reason:*\n{req.reason}"},
                    ],
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Approve ✅"},
                            "style": "primary",
                            "action_id": "saga_approve",
                            "value": json.dumps({"req_id": req.id, "action": "approve"}),
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Deny ❌"},
                            "style": "danger",
                            "action_id": "saga_deny",
                            "value": json.dumps({"req_id": req.id, "action": "deny"}),
                        },
                    ],
                },
            ]
        }

    @classmethod
    def handle_interactive_callback(cls, payload: dict[str, Any], approver: str = "slack_user") -> dict[str, Any]:
        actions = payload.get("actions", [])
        if not actions:
            return {"status": "ignored"}
        val = json.loads(actions[0].get("value", "{}"))
        req_id = val.get("req_id")
        action = val.get("action")

        if not req_id or not action:
            return {"status": "error", "message": "Invalid callback payload"}

        store = get_approval_store()
        granted = (action == "approve")
        res = store.decide(req_id, granted=granted, approver=approver)
        if not res:
            return {"status": "not_found", "req_id": req_id}

        return {"status": "resolved", "req_id": req_id, "granted": granted, "approver": approver}


__all__ = ["SlackBlockKitApp"]
