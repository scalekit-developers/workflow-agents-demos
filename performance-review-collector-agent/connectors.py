"""
Connector wrappers for Airtable, Google Forms, Notion (via NotionMCP), and Slack.

All APIs go through Scalekit's actions.execute_tool().
No direct API imports, no token management, no credential storage in code.

Tool names below are verified against the live Scalekit connector catalog
(search_tools) at build time -- not guessed:
  - airtable_list_records, airtable_get_base_schema      (AIRTABLE)
  - googleforms_get_form, googleforms_list_responses     (GOOGLEFORMS)
  - notionmcp_notion-create-pages, notionmcp_notion-update-page,
    notionmcp_notion-search                              (NOTIONMCP)
  - slack_send_message                                    (SLACK)
"""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ConnectorError(Exception):
    """Raised when a connector operation fails and there is no safe fallback."""


def _unwrap_mcp_envelope(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    MCP-based connectors (NOTIONMCP, SLACKMCP, AIRTABLEMCP, ...) return
    {"content": [{"type": "text", "text": "<json-or-plain-string>"}]} instead
    of a flat payload dict -- verified live against NOTIONMCP and SLACKMCP.
    Plain REST connectors (AIRTABLE, GOOGLEFORMS) return the flat payload
    directly, so this only unwraps when the envelope shape is actually present.
    """
    if not isinstance(data, dict) or "content" not in data:
        return data

    content = data.get("content")
    if not isinstance(content, list) or not content:
        return data

    text = content[0].get("text") if isinstance(content[0], dict) else None
    if text is None:
        return data

    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return {"text": text}


class Connector:
    """Base connector class -- shared auth-check and tool-execution logic."""

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
        except Exception as e:
            logger.error(f"Failed to check {self.connector_name} auth: {e}")
            return False

        if status != "ACTIVE":
            logger.warning(f"{self.connector_name} ({self.identifier}) -- {status}")
            try:
                link = self.actions.get_authorization_link(
                    connection_name=self.connector_name,
                    identifier=self.identifier,
                ).link
                logger.warning(f"Authorize here: {link}")
            except Exception:
                logger.warning("Check the Scalekit dashboard to authorize this connector")
            return False

        logger.info(f"✓ {self.connector_name} ({self.identifier}) -- ACTIVE")
        return True

    def execute_tool(self, tool_name: str, **kwargs) -> Dict[str, Any]:
        """Execute a Scalekit tool and return the data payload, unwrapping MCP envelopes."""
        try:
            result = self.actions.execute_tool(
                tool_name=tool_name,
                identifier=self.identifier,
                tool_input=kwargs,
            )
            return _unwrap_mcp_envelope(result.data or {})
        except Exception as e:
            logger.error(f"Tool execution failed: {tool_name}: {e}")
            raise ConnectorError(f"{tool_name} failed: {e}") from e


class AirtableConnector(Connector):
    """
    Airtable API operations -- structured review responses.

    connector_name must match the exact connection name shown in the Scalekit
    dashboard (e.g. "airtable-3j16TKTG"), not the generic "AIRTABLE" provider
    label -- Scalekit auto-suffixes connection names per workspace, and
    check_auth()'s get_or_create_connected_account call needs the exact name.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "airtable"):
        super().__init__(actions, connector_name, identifier)

    def list_all_records(
        self,
        base_id: str,
        table_name: str,
        view: str = "",
        page_size: int = 100,
    ) -> List[Dict]:
        """Fetch every record in a table, paginating via Airtable's offset token."""
        records: List[Dict] = []
        offset = None

        while True:
            kwargs: Dict[str, Any] = {
                "base_id": base_id,
                "table_id_or_name": table_name,
                "page_size": page_size,
            }
            if view:
                kwargs["view"] = view
            if offset:
                kwargs["offset"] = offset

            data = self.execute_tool("airtable_list_records", **kwargs) or {}
            batch = data.get("records") or []
            records.extend(batch)

            offset = data.get("offset")
            if not offset:
                break

        return records


class GoogleFormsConnector(Connector):
    """
    Google Forms API operations -- free-text feedback responses.

    connector_name must match the exact connection name shown in the Scalekit
    dashboard (e.g. "googleforms-WqF2XTWv"), same caveat as AirtableConnector.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "googleforms"):
        super().__init__(actions, connector_name, identifier)

    def get_form(self, form_id: str) -> Dict:
        """Fetch form structure (question IDs, titles) for mapping answers."""
        return self.execute_tool("googleforms_get_form", form_id=form_id)

    def list_all_responses(self, form_id: str, page_size: int = 100) -> List[Dict]:
        """Fetch every response submitted to a form, paginating via pageToken."""
        responses: List[Dict] = []
        page_token = None

        while True:
            kwargs: Dict[str, Any] = {"form_id": form_id, "page_size": page_size}
            if page_token:
                kwargs["page_token"] = page_token

            data = self.execute_tool("googleforms_list_responses", **kwargs) or {}
            batch = data.get("responses") or []
            responses.extend(batch)

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return responses


class NotionConnector(Connector):
    """Notion API operations (via the NotionMCP connector) -- summary pages."""

    def __init__(self, actions, identifier: str, connector_name: str = "NOTIONMCP"):
        super().__init__(actions, connector_name, identifier)

    def find_existing_child_page(self, parent_page_id: str, title: str) -> Optional[str]:
        """Search for a page with this exact title under the workspace. Returns page ID or None."""
        try:
            data = self.execute_tool("notionmcp_notion-search", query=title)
        except ConnectorError:
            return None

        for result in data.get("results", []) or []:
            if result.get("title") == title:
                return result.get("id")
        return None

    def create_employee_page(
        self,
        parent_page_id: str,
        title: str,
        markdown_body: str,
    ) -> Dict:
        """Create a new page under parent_page_id with the given title and markdown content."""
        return self.execute_tool(
            "notionmcp_notion-create-pages",
            parent={"type": "page_id", "page_id": parent_page_id},
            pages=[
                {
                    "properties": {"title": title},
                    "content": markdown_body,
                }
            ],
        )

    def update_employee_page(self, page_id: str, markdown_body: str) -> Dict:
        """Overwrite an existing employee page's content with the latest summary."""
        return self.execute_tool(
            "notionmcp_notion-update-page",
            page_id=page_id,
            command="replace_content",
            new_str=markdown_body,
        )

    def upsert_employee_page(self, parent_page_id: str, title: str, markdown_body: str) -> Dict:
        """Create the employee's page if it doesn't exist yet, otherwise update it in place."""
        existing_id = self.find_existing_child_page(parent_page_id, title)
        if existing_id:
            logger.info(f"Updating existing Notion page for {title}")
            return self.update_employee_page(existing_id, markdown_body)

        logger.info(f"Creating new Notion page for {title}")
        return self.create_employee_page(parent_page_id, title, markdown_body)


class SlackConnector(Connector):
    """
    Slack API operations -- manager notification.

    Scalekit exposes two Slack connector variants with different send-message
    tool signatures (verified live):
      - "*mcp*"  variant -> slackmcp_slack_send_message(channel_id=..., message=...)
      - anything else     -> slack_send_message(channel=..., text=...)
    Connection names are workspace-specific (e.g. "slackmcp", "slack-sKfekCVz"),
    so the MCP-vs-plain distinction is detected by substring match on "mcp"
    rather than an exact name lookup.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "slackmcp"):
        super().__init__(actions, connector_name, identifier)
        if "mcp" in connector_name.lower():
            self._send_tool = "slackmcp_slack_send_message"
            self._channel_param = "channel_id"
            self._text_param = "message"
        else:
            self._send_tool = "slack_send_message"
            self._channel_param = "channel"
            self._text_param = "text"

    def send_dm(self, user_id: str, text: str) -> Dict:
        """Send a direct message. Passing a user ID as the channel targets a DM."""
        kwargs = {self._channel_param: user_id, self._text_param: text}
        return self.execute_tool(self._send_tool, **kwargs)


def _extract_page_title(page: Dict) -> str:
    """Pull the plain-text title out of a Notion page/search-result object."""
    props = page.get("properties", {})
    for key in ("title", "Title", "Name", "name"):
        prop = props.get(key, {})
        title_list = prop.get("title", [])
        if title_list:
            return "".join(item.get("plain_text", "") for item in title_list)

    for prop in props.values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            title_list = prop.get("title", [])
            if title_list:
                return "".join(item.get("plain_text", "") for item in title_list)

    return page.get("title", "") or ""
