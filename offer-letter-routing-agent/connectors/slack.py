"""Slack connector — route offer approvals to hiring managers."""
import logging
from typing import Optional, Any

logger = logging.getLogger("offer-letter-agent")


class SlackConnector:
    """Approval routing and notifications."""

    def __init__(self, connect: Any, user_id: str, connection_name: str = "slack"):
        """Initialize with Scalekit connect client, user ID, and connection name.

        connection_name must be passed through to execute_tool — without it,
        Scalekit disambiguates by identifier alone, which fails with
        "multiple connected accounts found" whenever the same identifier is
        registered under more than one Slack connection (a real, deployed
        constellation of connector names like "slack", "slack-sKfekCVz").
        """
        self.connect = connect
        self.user_id = user_id
        self.connection_name = connection_name

    def send_message(self, channel: str, text: str) -> Optional[str]:
        """Post a message to a Slack channel or DM. Returns message timestamp."""
        try:
            result = self.connect.execute_tool(
                tool_name="slack_send_message",
                identifier=self.user_id,
                connection_name=self.connection_name,
                tool_input={"channel": channel, "text": text},
            )
            ts = (result.data or {}).get("timestamp", "")
            if ts:
                logger.info(f"Posted to Slack (channel={channel}, ts={ts})")
            return ts
        except Exception as e:
            logger.error(f"Failed to post to Slack: {e}")
            return None

    def format_approval_request(
        self,
        candidate_name: str,
        role_title: str,
        base_salary: str,
        start_date: str,
        document_id: str,
        document_url: str,
    ) -> str:
        """Format an offer approval request for the hiring manager."""
        return (
            f"📝 *Offer Approval Needed*\n\n"
            f"*Candidate:* {candidate_name}\n"
            f"*Role:* {role_title}\n"
            f"*Base salary:* {base_salary}\n"
            f"*Start date:* {start_date}\n\n"
            f"*Document:* {document_url or document_id}\n\n"
            f"Reply in this thread to approve, or flag changes needed."
        )

    def format_sent_notification(
        self, candidate_name: str, candidate_email: str, role_title: str
    ) -> str:
        """Format a notification that the offer was delivered to the candidate."""
        return (
            f"✅ *Offer Delivered*\n\n"
            f"Offer for *{role_title}* sent to {candidate_name} ({candidate_email}) "
            f"for e-signature via PandaDoc."
        )
