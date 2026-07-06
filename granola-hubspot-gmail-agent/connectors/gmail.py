"""Gmail connector — creates email drafts via Scalekit gmail_create_draft tool."""
import logging
from typing import Any, Optional

logger = logging.getLogger("granola-hubspot")


def create_draft(
    connect: Any,
    user_id: str,
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    content_type: str = "text/plain",
) -> Optional[dict]:
    """Create a Gmail draft using Scalekit gmail_create_draft tool.

    Args:
        connect: Scalekit connect client
        user_id: User identifier (email)
        to: Recipient email address
        subject: Email subject
        body: Email body (plain text or HTML)
        cc: Optional CC recipient
        bcc: Optional BCC recipient
        content_type: "text/plain" or "text/html" (default: "text/plain")

    Returns the created draft object including its id, or None on failure.
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
            tool_name="gmail_create_draft",
            identifier=user_id,
            tool_input=tool_input,
        )
        draft_id = (result.data or {}).get("message", {}).get("id")
        if draft_id:
            logger.debug(f"Draft created for {to} (id={draft_id})")
        return result.data or {}
    except Exception as e:
        logger.error(f"Failed to create Gmail draft: {e}")
        return None
