"""Granola MCP connector — fetch meetings, transcripts, and notes."""
import json
import re
import logging
from typing import Optional, Any

logger = logging.getLogger("granola-hubspot")


class GranolaConnector:
    """Granola meeting data fetcher."""

    def __init__(self, connect: Any, user_id: str):
        """Initialize with Scalekit connect client and user ID."""
        self.connect = connect
        self.user_id = user_id

    def list_meetings(self, limit: int = 3) -> list[dict]:
        """Fetch recent meetings from Granola."""
        try:
            result = self.connect.execute_tool(
                tool_name="granolamcp_list_meetings",
                identifier=self.user_id,
                tool_input={"limit": limit},
            )
            meetings_text = "".join(
                c.get("text", "")
                for c in (result.data or {}).get("content", [])
                if isinstance(c, dict)
            )
            meeting_ids = re.findall(r'id="([^"]+)"', meetings_text)
            meeting_titles = re.findall(r'title="([^"]+)"', meetings_text)
            return [
                {"id": mid, "title": title}
                for mid, title in zip(meeting_ids, meeting_titles)
            ]
        except Exception as e:
            logger.error(f"Failed to list meetings: {e}")
            return []

    def get_transcript(self, meeting_id: str) -> str:
        """Fetch meeting transcript from Granola."""
        try:
            result = self.connect.execute_tool(
                tool_name="granolamcp_get_meeting_transcript",
                identifier=self.user_id,
                tool_input={"meeting_id": meeting_id},
            )
            tx_raw = "".join(
                c.get("text", "")
                for c in (result.data or {}).get("content", [])
                if isinstance(c, dict)
            ).strip()

            try:
                return json.loads(tx_raw).get("transcript") or ""
            except (json.JSONDecodeError, AttributeError):
                return tx_raw
        except Exception as e:
            logger.debug(f"Failed to get transcript for {meeting_id}: {e}")
            return ""

    def query_notes(self, query: str) -> str:
        """Query Granola meeting notes as fallback."""
        try:
            result = self.connect.execute_tool(
                tool_name="granolamcp_query_granola_meetings",
                identifier=self.user_id,
                tool_input={"query": query},
            )
            return "".join(
                c.get("text", "")
                for c in (result.data or {}).get("content", [])
                if isinstance(c, dict)
            ).strip()
        except Exception as e:
            logger.debug(f"Failed to query notes: {e}")
            return ""

    def fetch_meeting_content(self, meeting_id: str, title: str) -> Optional[str]:
        """Fetch transcript or notes for a meeting, with fallback."""
        content = self.get_transcript(meeting_id)
        if len(content) < 30:
            logger.debug("Transcript empty — querying note content")
            content = self.query_notes(title)
        if len(content) < 30:
            logger.warning(f"No content found for {title}")
            return None
        logger.debug(f"Content: {len(content)} chars")
        return content
