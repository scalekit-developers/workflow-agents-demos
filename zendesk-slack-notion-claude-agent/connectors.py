"""
Connector wrappers for Zendesk, Slack, and Notion.

All APIs go through Scalekit's execute_tool().
No direct API imports or token management.
"""

import logging
from typing import Dict, List, Any

logger = logging.getLogger(__name__)


class Connector:
    """Base connector class."""

    def __init__(self, actions, connector_name: str, identifier: str):
        self.actions = actions
        self.connector_name = connector_name
        self.identifier = identifier

    def check_auth(self) -> bool:
        """Check if connector is authorized. Returns True if ACTIVE."""
        try:
            resp = self.actions.get_or_create_connected_account(
                connection_name=self.connector_name,
                identifier=self.identifier,
            )
            status = resp.connected_account.status
            if status != "ACTIVE":
                logger.warning(f"{self.connector_name} ({self.identifier}) -- {status}")
                try:
                    link = self.actions.get_authorization_link(
                        connection_name=self.connector_name,
                        identifier=self.identifier,
                    ).link
                    logger.warning(f"Authorize here: {link}")
                except Exception:
                    logger.warning(f"Check Scalekit dashboard to authorize this connector")
                return False
            else:
                logger.info(f"✓ {self.connector_name} ({self.identifier}) -- ACTIVE")
                return True
        except Exception as e:
            logger.error(f"Failed to check {self.connector_name} auth: {e}")
            return False

    def execute_tool(self, tool_name: str, **kwargs) -> Dict[str, Any]:
        """Execute a Scalekit tool and return the data payload."""
        try:
            result = self.actions.execute_tool(
                tool_name=tool_name,
                identifier=self.identifier,
                tool_input=kwargs,
            )
            return result.data or {}
        except Exception as e:
            logger.error(f"Tool execution failed: {tool_name}: {e}")
            raise


class ZendeskConnector(Connector):
    """Zendesk API operations."""

    def __init__(self, actions, identifier: str):
        super().__init__(actions, "zendesk", identifier)

    def search_tickets(self, query: str = "type:ticket status:new status:open") -> List[Dict]:
        """Fetch tickets matching query."""
        try:
            data = self.execute_tool(
                "zendesk_search_tickets",
                query=query,
                sort_by="created_at",
                sort_order="desc",
            )
        except Exception:
            # Fallback
            try:
                data = self.execute_tool(
                    "zendesk_tickets_list",
                    sort_by="created_at",
                    sort_order="desc",
                )
            except Exception as e:
                logger.error(f"Failed to fetch tickets: {e}")
                return []

        return data.get("results") or data.get("tickets") or data.get("data") or []

    def get_ticket(self, ticket_id: str) -> Dict:
        """Fetch full ticket details."""
        try:
            data = self.execute_tool("zendesk_ticket_get", ticket_id=str(ticket_id))
            return data.get("ticket", data)
        except Exception as e:
            logger.warning(f"Failed to fetch ticket #{ticket_id}: {e}")
            return {}

    def update_ticket(self, ticket_id: int, tags: List[str], priority: str) -> None:
        """Update ticket tags and priority."""
        try:
            self.execute_tool(
                "zendesk_ticket_update",
                ticket_id=ticket_id,
                tags=tags,
                priority=priority,
            )
        except Exception as e:
            logger.warning(f"Failed to update ticket tags/priority: {e}")

    def add_internal_note(self, ticket_id: str, body: str) -> None:
        """Add internal note to ticket."""
        try:
            self.execute_tool(
                "zendesk_ticket_reply",
                ticket_id=str(ticket_id),
                body=body,
                public=False,
            )
        except Exception as e:
            logger.warning(f"Failed to add internal note: {e}")


class SlackConnector(Connector):
    """Slack API operations."""

    def __init__(self, actions, connector_name: str, identifier: str):
        super().__init__(actions, connector_name, identifier)

    def send_message(self, channel: str, text: str) -> Dict:
        """Post message to channel."""
        try:
            result = self.execute_tool(
                "slack_send_message",
                channel=channel,
                text=text,
            )
            return result
        except Exception as e:
            logger.warning(f"Failed to post to {channel}: {e}")
            return {}


class NotionConnector(Connector):
    """Notion API operations."""

    def __init__(self, actions, identifier: str):
        super().__init__(actions, "notion", identifier)

    def search_kb(self, query: str, database_id: str = "") -> List[Dict]:
        """Search knowledge base for matching articles."""
        if not query:
            return []

        if database_id:
            try:
                data = self.execute_tool(
                    "notion_database_query",
                    database_id=database_id,
                    query=query,
                )
                return data.get("results") or data.get("pages") or data.get("data") or []
            except Exception:
                pass

        # Fallback: page search
        try:
            data = self.execute_tool("notion_page_search", query=query)
            return data.get("results") or data.get("pages") or data.get("data") or []
        except Exception as e:
            logger.warning(f"Notion search failed: {e}")
            return []

    def extract_articles(self, pages: List[Dict]) -> List[Dict]:
        """Extract article metadata from Notion pages."""
        articles = []
        for page in pages[:5]:
            title = self._extract_title(page)
            url = page.get("url") or page.get("public_url") or ""
            page_id = page.get("id", "")

            snippet = self._extract_snippet(page)

            articles.append({
                "title": title or f"Page {page_id[:8]}",
                "url": url,
                "snippet": snippet,
                "page_id": page_id,
            })

        return articles

    @staticmethod
    def _extract_title(page: Dict) -> str:
        """Pull title from Notion page."""
        props = page.get("properties", {})

        for key in ("Name", "Title", "name", "title"):
            prop = props.get(key, {})
            title_list = prop.get("title", [])
            if title_list:
                return title_list[0].get("plain_text", "")

        for prop in props.values():
            if isinstance(prop, dict) and prop.get("type") == "title":
                title_list = prop.get("title", [])
                if title_list:
                    return title_list[0].get("plain_text", "")

        return ""

    @staticmethod
    def _extract_snippet(page: Dict) -> str:
        """Extract snippet from Notion page."""
        props = page.get("properties", {})
        for key in ("Description", "Summary", "Content", "Excerpt"):
            prop = props.get(key, {})
            rich_text = prop.get("rich_text", [])
            if rich_text:
                return rich_text[0].get("plain_text", "")[:200]
        return ""
