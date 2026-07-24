"""
Connector wrappers for Salesforce, HubSpot, Slack (via SlackMCP), and Google Sheets.

All APIs go through Scalekit's actions.execute_tool(). No direct API imports,
no token management, no credential storage in code.

Tool names and parameter shapes below are verified live against this workspace's
Scalekit environment (env_20324953475777334) at build time -- not guessed:
  - salesforce_soql_execute(soql_query=...)                    (SALESFORCE, connector "salesforce-1")
      -> flat dict: {"totalSize": N, "records": [...], "done": true}
  - hubspot_deal_pipelines_list(objectType="deals")             (HUBSPOT, connector "hubspot")
  - hubspot_deals_search(filterGroups=[...], properties=[...])  (HUBSPOT)
      -> flat dict: {"total": N, "results": [...]}. Note: HUBSPOTMCP also exists
         in the Scalekit tool catalog (search_crm_objects etc.) but has NO
         connected account in this workspace (only plain "hubspot" is ACTIVE),
         so this agent uses the plain HUBSPOT connector exclusively.
  - slackmcp_slack_search_channels(query=...),
    slackmcp_slack_send_message(channel_id=..., message=...)    (SLACKMCP)
      -> MCP envelope: {"content": [{"type": "text", "text": "<json-string>"}]}
  - googlesheets_read_spreadsheet(spreadsheet_id=...),
    googlesheets_add_sheet(spreadsheet_id=..., title=...),
    googlesheets_append_values(spreadsheet_id=..., range=...,
        value_input_option=..., values=[[...]]),                (GOOGLESHEETS)
      -> flat dict, no envelope. Note value_input_option is snake_case even
         though the upstream Google API calls it valueInputOption -- verified
         live; the camelCase form returns "'valueInputOption' is required".
"""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ConnectorError(Exception):
    """Raised when a connector operation fails and there is no safe fallback."""


def _unwrap_mcp_envelope(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    MCP-based connectors (SLACKMCP, ...) return
    {"content": [{"type": "text", "text": "<json-or-plain-string>"}]} instead
    of a flat payload dict -- verified live against SLACKMCP. Plain REST
    connectors (SALESFORCE, HUBSPOT, GOOGLESHEETS) return the flat payload
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

        logger.info(f"[OK] {self.connector_name} ({self.identifier}) -- ACTIVE")
        return True

    def execute_tool(self, tool_name: str, **kwargs) -> Dict[str, Any]:
        """Execute a Scalekit tool and return the data payload, unwrapping MCP envelopes.

        Passes connection_name explicitly (not just identifier) because a
        single identifier can be connected to multiple connectors of the same
        provider type in one workspace, which makes tool_name-based resolution
        ambiguous (INVALID_ARGUMENT: "multiple connected accounts found").
        """
        try:
            result = self.actions.execute_tool(
                tool_name=tool_name,
                identifier=self.identifier,
                connection_name=self.connector_name,
                tool_input=kwargs,
            )
            return _unwrap_mcp_envelope(result.data or {})
        except Exception as e:
            logger.error(f"Tool execution failed: {tool_name}: {e}")
            raise ConnectorError(f"{tool_name} failed: {e}") from e


class SalesforceConnector(Connector):
    """Salesforce API operations -- open pipeline by stage via SOQL."""

    def __init__(self, actions, identifier: str, connector_name: str = "salesforce-1"):
        super().__init__(actions, connector_name, identifier)

    def list_open_opportunities(self, extra_where: str = "") -> List[Dict]:
        """
        Fetch every open (not closed) Opportunity with the fields needed for
        coverage and at-risk analysis: Id, Name, StageName, Amount, CloseDate.

        Salesforce SOQL query results are capped at 2000 rows per page by the
        API; salesforce_query_next_page exists for pagination, but a single
        page is enough for typical open-pipeline sizes in this use case, so
        pagination is not implemented here to keep the query surface small
        (documented as a known scaling limit, see README).
        """
        where = "WHERE IsClosed = false"
        if extra_where:
            where += f" AND {extra_where}"

        query = (
            "SELECT Id, Name, StageName, Amount, CloseDate, IsClosed "
            f"FROM Opportunity {where} ORDER BY CloseDate ASC LIMIT 2000"
        )
        data = self.execute_tool("salesforce_soql_execute", soql_query=query) or {}
        return data.get("records") or []


class HubSpotConnector(Connector):
    """
    HubSpot API operations -- open deals by stage.

    Uses the plain "hubspot" REST connector exclusively. HUBSPOTMCP tools
    (e.g. hubspotmcp_search_crm_objects) exist in the Scalekit tool catalog
    but this workspace has no connected account for that connector variant,
    only the plain HUBSPOT connector is ACTIVE -- verified live via
    list_connected_accounts before writing this code.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "hubspot"):
        super().__init__(actions, connector_name, identifier)

    def list_deal_pipelines(self) -> List[Dict]:
        """Fetch all deal pipelines and their stages (id, label, isClosed metadata)."""
        data = self.execute_tool("hubspot_deal_pipelines_list", objectType="deals") or {}
        return data.get("results") or []

    def list_open_deals(self, open_stage_ids: List[str], limit: int = 100) -> List[Dict]:
        """
        Fetch deals whose dealstage is one of the given open-stage IDs,
        paginating via offset until exhausted.
        """
        if not open_stage_ids:
            return []

        deals: List[Dict] = []
        offset = 0
        properties = ["dealname", "amount", "dealstage", "closedate", "pipeline"]

        while True:
            data = self.execute_tool(
                "hubspot_deals_search",
                filterGroups=[
                    {
                        "filters": [
                            {
                                "propertyName": "dealstage",
                                "operator": "IN",
                                "values": open_stage_ids,
                            }
                        ]
                    }
                ],
                properties=properties,
                limit=limit,
                offset=offset,
            ) or {}
            batch = data.get("results") or []
            deals.extend(batch)

            total = data.get("total", 0)
            offset += len(batch)
            if not batch or offset >= total:
                break

        return deals


class SlackConnector(Connector):
    """
    Slack API operations (via the SlackMCP connector) -- resolve a channel
    name to an ID and post forecast commentary.

    Only the SLACKMCP connector variant is ACTIVE in this workspace (the
    plain SLACK connector is PENDING_AUTH) -- verified live. Send-message
    params are channel_id/message, not channel/text.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "slackmcp"):
        super().__init__(actions, connector_name, identifier)

    def resolve_channel_id(self, channel_name_or_id: str) -> Optional[str]:
        """
        Resolve a channel like "#revenue-ops" to its Slack channel ID.
        If channel_name_or_id already looks like a raw ID (C..., D..., G..., U...),
        it's used as-is without a search round-trip.
        """
        if not channel_name_or_id:
            return None

        stripped = channel_name_or_id.lstrip("#")
        if channel_name_or_id[:1] in ("C", "D", "G", "U") and channel_name_or_id.isalnum():
            return channel_name_or_id

        data = self.execute_tool("slackmcp_slack_search_channels", query=stripped) or {}
        # SlackMCP's search tools return a markdown-formatted "results" string,
        # not structured JSON -- verified live. Parse the "Permalink" archive
        # URL's channel ID segment out of the first matching result block.
        results_text = data.get("results", "") if isinstance(data, dict) else ""
        return _extract_first_channel_id(results_text, stripped)

    def send_message(self, channel_id: str, text: str) -> Dict:
        """Post a message to a channel or DM. Passing a user ID as channel_id sends a DM."""
        return self.execute_tool("slackmcp_slack_send_message", channel_id=channel_id, message=text)


class GoogleSheetsConnector(Connector):
    """
    Google Sheets API operations -- append pipeline snapshot + commentary rows.

    There is no tool to create a brand-new spreadsheet from an empty state in
    a way this agent relies on for the default flow (googlesheets_create_spreadsheet
    does exist and was used once, live, to provision a fresh test destination
    -- see README) -- the normal/documented flow expects GOOGLE_SHEETS_SPREADSHEET_ID
    to already point at an existing spreadsheet, and this connector's job is
    only to ensure the destination TAB exists within it (mirrors the reference
    repo's ensure_airtable_table() pattern for the sibling agent's "base must
    already exist" constraint).
    """

    def __init__(self, actions, identifier: str, connector_name: str = "googlesheets-BOzvgKS0"):
        super().__init__(actions, connector_name, identifier)

    def list_sheet_titles(self, spreadsheet_id: str) -> List[str]:
        """Return the tab/sheet titles present in the spreadsheet."""
        data = self.execute_tool("googlesheets_read_spreadsheet", spreadsheet_id=spreadsheet_id) or {}
        sheets = data.get("sheets") or []
        return [s.get("properties", {}).get("title", "") for s in sheets]

    def ensure_tab(self, spreadsheet_id: str, tab_name: str) -> bool:
        """Create the tab if it doesn't already exist. Returns True if it was created."""
        existing = self.list_sheet_titles(spreadsheet_id)
        if tab_name in existing:
            return False
        self.execute_tool("googlesheets_add_sheet", spreadsheet_id=spreadsheet_id, title=tab_name)
        return True

    def append_row(self, spreadsheet_id: str, tab_name: str, row: List[Any]) -> Dict:
        """Append a single row after the last row with content in the tab."""
        return self.execute_tool(
            "googlesheets_append_values",
            spreadsheet_id=spreadsheet_id,
            range=f"{tab_name}!A1",
            value_input_option="USER_ENTERED",
            values=[row],
        )

    def append_header_if_empty(self, spreadsheet_id: str, tab_name: str, header: List[str]) -> None:
        """Write the header row only if the tab currently has no rows at all."""
        data = self.execute_tool(
            "googlesheets_get_values",
            spreadsheet_id=spreadsheet_id,
            range=f"{tab_name}!A1:A1",
        ) or {}
        if data.get("values"):
            return
        self.append_row(spreadsheet_id, tab_name, header)


def _extract_first_channel_id(results_text: str, query: str) -> Optional[str]:
    """
    Parse a Slack channel ID out of SlackMCP's markdown search-results text,
    e.g. "...Permalink: [link](https://workspace.slack.com/archives/C09K0K2RZ6Y)...".
    Returns None if no channel block is present (e.g. "No results found.").
    """
    import re

    match = re.search(r"/archives/([A-Z0-9]+)", results_text or "")
    if match:
        return match.group(1)
    return None
