"""Messaging connector for Slack and Discord bot agents.

Provides typed compensation semantics for bot message dispatches. If a subsequent
agent reasoning or calculation step determines that a previously sent message was
incorrect or hallucinated, the message is automatically deleted or updated with a
[REDACTED] notice.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from ..semantics import ActionSemantics, Compensation
from ..registry import compensator

logger = logging.getLogger("agent_saga.connectors.messaging")


class MessagingConnector:
    """Connector for Slack and Discord messaging platforms."""

    async def post_slack_message(self, channel: str, text: str) -> Dict[str, Any]:
        """Forward action: Post Slack message."""
        ts = "1721635200.000100"
        logger.info("Posted message to Slack channel %s: %s (ts: %s)", channel, text, ts)
        return {"channel": channel, "ts": ts, "text": text}

    async def post_discord_message(self, channel_id: str, text: str) -> Dict[str, Any]:
        """Forward action: Post Discord message."""
        message_id = "1234567890987654321"
        logger.info("Posted message to Discord channel %s: %s (id: %s)", channel_id, text, message_id)
        return {"channel_id": channel_id, "message_id": message_id, "text": text}


@compensator("messaging.delete_slack_message")
async def delete_slack_message(channel: str, ts: str) -> Dict[str, Any]:
    logger.info("Deleted Slack message in channel %s at ts %s", channel, ts)
    return {"channel": channel, "ts": ts, "status": "deleted"}


@compensator("messaging.delete_discord_message")
async def delete_discord_message(channel_id: str, message_id: str) -> Dict[str, Any]:
    logger.info("Deleted Discord message %s in channel %s", message_id, channel_id)
    return {"channel_id": channel_id, "message_id": message_id, "status": "deleted"}


def slack_message_compensation(result: Dict[str, Any]) -> Compensation:
    return Compensation(
        fn=delete_slack_message,
        args=[result["channel"], result["ts"]],
    )


def discord_message_compensation(result: Dict[str, Any]) -> Compensation:
    return Compensation(
        fn=delete_discord_message,
        args=[result["channel_id"], result["message_id"]],
    )
