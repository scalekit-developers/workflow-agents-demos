"""Gmail connector — deliver offer documents and notifications to candidates."""
import logging
from typing import Any, Optional

logger = logging.getLogger("offer-letter-agent")


def send_message(
    connect: Any,
    user_id: str,
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    content_type: str = "text/plain",
    connection_name: str = "gmail",
) -> Optional[dict]:
    """Send an email immediately via the Scalekit gmail_send_message tool.

    Args:
        connect: Scalekit connect client
        user_id: Recruiter identity (email) the send is scoped to
        to: Recipient email address (the candidate)
        subject: Email subject
        body: Email body (plain text or HTML)
        cc: Optional CC recipient (e.g. hiring manager)
        bcc: Optional BCC recipient
        content_type: "text/plain" or "text/html" (default: "text/plain")
        connection_name: Scalekit connection name. Passed explicitly to
            execute_tool — without it, Scalekit disambiguates by identifier
            alone, which fails with "multiple connected accounts found" if
            that identifier is registered under more than one connection.

    Returns the sent message object, or None on failure.
    """
    try:
        tool_input = {
            "to": to,
            "subject": subject,
            "body": body,
            "content_type": content_type,
        }
        if cc:
            tool_input["cc"] = cc
        if bcc:
            tool_input["bcc"] = bcc

        result = connect.execute_tool(
            tool_name="gmail_send_message",
            identifier=user_id,
            connection_name=connection_name,
            tool_input=tool_input,
        )
        data = result.data or {}
        message_id = data.get("id")
        if message_id:
            logger.info(f"Email sent to {to} (id={message_id})")
        return data
    except Exception as e:
        logger.error(f"Failed to send email to {to}: {e}")
        return None
