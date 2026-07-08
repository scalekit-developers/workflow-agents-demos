"""Slack connector for sending risk reports."""


class SlackConnector:
    """Send messages and reports to Slack."""

    def __init__(self, connect, user_id: str):
        self.connect = connect
        self.user_id = user_id

    def send_message(self, channel: str, text: str) -> dict:
        """Send a message to a Slack channel."""
        result = self.connect.execute_tool(
            tool_name="slack_send_message",
            identifier=self.user_id,
            tool_input={"channel": channel, "text": text},
            connection_name="slack",
        )
        return result.data or {}

    def send_dm(self, user_id: str, text: str) -> dict:
        """Send a direct message to a Slack user."""
        result = self.connect.execute_tool(
            tool_name="slack_send_message",
            identifier=self.user_id,
            tool_input={"channel": user_id, "text": text},
            connection_name="slack",
        )
        return result.data or {}

    def format_risk_report(self, calls_analysis: list) -> str:
        """Format analyzed calls into a risk report."""
        if not calls_analysis:
            return "No calls analyzed today."

        report = "DEAL RISK REPORT\n" + "=" * 50 + "\n\n"
        for item in sorted(calls_analysis, key=lambda x: x.get("risk_score", 0), reverse=True):
            report += f"Company: {item.get('company', 'Unknown')}\n"
            report += f"Risk Score: {item.get('risk_score', 'N/A')}\n"
            report += f"Sentiment: {item.get('sentiment', 'N/A')}\n"
            report += f"Engagement: {item.get('engagement_level', 'N/A')}\n"
            if item.get("objections"):
                report += f"Objections: {', '.join(item['objections'][:2])}\n"
            report += "\n"

        return report
