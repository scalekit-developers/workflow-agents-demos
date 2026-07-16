"""SlackMCP connector — the read-capable Slack connector (reactions, threads).

The plain SLACK connector (see connectors/slack.py) only supports sending
messages — it has no tool to read reactions, threads, or channel history.
Only SLACKMCP exposes slack_get_reactions / slack_read_thread / etc., so a
real approval gate (poll for a manager's reaction) requires this connector
to be set up in Scalekit as its own connection (e.g. "slackmcp").

Verified live against SLACKMCP's actual MCP server (2026-07-15):
  - slack_send_message wants "message", not "text" (differs from the plain
    SLACK connector's schema).
  - Every tool's response is wrapped as {"content": [{"type": "text",
    "text": "<JSON-encoded string>"}]} — the inner text must be json.loads'd,
    it is not returned as structured data directly.
  - slack_get_reactions returns a human-readable sentence, not a list, e.g.
    "Reactions on message:\\n\\n:white_check_mark: × 1 — Name (UID)" or
    "No reactions found on this message." when empty — parsed with regex.
    The reactor's Slack user id is captured too, since the approval gate
    needs to check *who* reacted, not just which emoji was used.
"""
import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger("offer-letter-agent")

_REACTION_LINE_RE = re.compile(r":(?P<emoji>[\w+-]+):\s*×\s*\d+.*?\((?P<user_id>[UW]\w+)\)")


class SlackMCPConnector:
    """Send-and-read Slack operations backed by the SLACKMCP connector."""

    def __init__(self, connect: Any, user_id: str, connection_name: str = "slackmcp"):
        self.connect = connect
        self.user_id = user_id
        self.connection_name = connection_name

    def _call(self, tool_name: str, tool_input: dict) -> Optional[dict]:
        """Call a slackmcp_* tool and unwrap its {"content": [{"text": "<json>"}]} envelope."""
        try:
            result = self.connect.execute_tool(
                tool_name=tool_name,
                identifier=self.user_id,
                connection_name=self.connection_name,
                tool_input=tool_input,
            )
            data = result.data or {}
            content = data.get("content") or []
            if not content:
                return {}
            text = content[0].get("text", "{}")
            return json.loads(text)
        except Exception as e:
            logger.error(f"SlackMCP call {tool_name} failed: {e}")
            return None

    def send_message(self, channel_id: str, message: str) -> Optional[dict]:
        """Send a message and return {"message_ts": ..., "channel_id": ...} on success."""
        parsed = self._call(
            "slackmcp_slack_send_message",
            {"channel_id": channel_id, "message": message},
        )
        if parsed is None:
            return None
        ctx = parsed.get("message_context", {})
        if ctx.get("message_ts"):
            logger.info(f"Posted to Slack via slackmcp (channel={channel_id}, ts={ctx['message_ts']})")
        return ctx or None

    def get_reactions(self, channel_id: str, message_ts: str) -> list[tuple[str, str]]:
        """Return (emoji, reactor_user_id) pairs for a message (empty if none).

        Reactor identity is required so the approval gate can check that the
        reaction actually came from the configured hiring manager, not just
        anyone in the channel (including, in principle, the candidate).
        """
        parsed = self._call(
            "slackmcp_slack_get_reactions",
            {"channel_id": channel_id, "message_ts": message_ts},
        )
        if parsed is None:
            return []
        text = parsed.get("result", "")
        if "No reactions found" in text:
            return []
        return _REACTION_LINE_RE.findall(text)

    def get_reaction_emojis(self, channel_id: str, message_ts: str) -> list[str]:
        """Return just the emoji names reacted on a message, ignoring reactor identity.

        Prefer get_reactions() for anything approval-related — this exists
        only for callers that genuinely don't care who reacted.
        """
        return [emoji for emoji, _user_id in self.get_reactions(channel_id, message_ts)]
