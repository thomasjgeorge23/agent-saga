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

    @classmethod
    def build_oauth_install_url(
        cls, client_id: str, redirect_uri: str, scopes: Optional[list[str]] = None
    ) -> str:
        """Construct a Slack OAuth 2.0 Install URL for workspace authorization."""
        from urllib.parse import urlencode
        scope_str = ",".join(scopes or ["chat:write", "commands", "incoming-webhook"])
        params = {
            "client_id": client_id,
            "scope": scope_str,
            "redirect_uri": redirect_uri,
        }
        return f"https://slack.com/oauth/v2/authorize?{urlencode(params)}"

    @classmethod
    def handle_slash_command(
        cls, command_text: str, user_name: str = "slack_user"
    ) -> dict[str, Any]:
        """Handle /saga-approve <approve|deny> <req_id> [note] slash commands."""
        parts = command_text.strip().split(maxsplit=2)
        if not parts:
            return {"response_type": "ephemeral", "text": "Usage: /saga-approve [approve|deny] <req_id> [note]"}

        action = parts[0].lower()
        if action in ("approve", "deny") and len(parts) >= 2:
            req_id = parts[1]
            note = parts[2] if len(parts) > 2 else ""
            granted = (action == "approve")
        else:
            req_id = parts[0]
            granted = True
            note = parts[1] if len(parts) > 1 else ""

        store = get_approval_store()
        res = store.decide(req_id, granted=granted, approver=user_name, note=note)
        if not res:
            return {"response_type": "ephemeral", "text": f"❌ Approval request `{req_id}` not found or already decided."}

        status_str = "APPROVED ✅" if granted else "DENIED ❌"
        return {"response_type": "in_channel", "text": f"{status_str} request `{req_id}` by @{user_name}."}


__all__ = ["SlackBlockKitApp"]
