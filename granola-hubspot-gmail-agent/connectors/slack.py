"""Slack connector — post messages to channels."""
import logging
from typing import Optional, Any

logger = logging.getLogger("granola-hubspot")


class SlackConnector:
    """Slack messaging."""

    def __init__(self, connect: Any, user_id: str):
        """Initialize with Scalekit connect client and user ID."""
        self.connect = connect
        self.user_id = user_id

    def send_message(self, channel: str, text: str) -> Optional[str]:
        """Post a message to a Slack channel. Returns message timestamp."""
        try:
            result = self.connect.execute_tool(
                tool_name="slack_send_message",
                identifier=self.user_id,
                tool_input={"channel": channel, "text": text},
            )
            ts = (result.data or {}).get("timestamp", "")
            if ts:
                logger.info(f"Posted to Slack (ts={ts})")
            return ts
        except Exception as e:
            logger.error(f"Failed to post to Slack: {e}")
            return None

    def format_summary(
        self,
        title: str,
        summary: str,
        next_step: str,
        action_items: list[str],
        deal_name: str,
        deal_id: str,
    ) -> str:
        """Format meeting summary for Slack."""
        action_str = "\n".join(f"• {a}" for a in action_items) if action_items else "• None"
        return (
            f"📞 *{title}*\n\n"
            f"{summary}\n\n"
            f"*Next Step:* {next_step}\n\n"
            f"*Action Items:*\n{action_str}\n\n"
            f"*Deal:* {deal_name} (id={deal_id})"
        )
