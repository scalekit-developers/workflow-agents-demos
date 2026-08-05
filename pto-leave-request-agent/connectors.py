"""
Connector wrappers for Gusto (via GustoMCP), Google Calendar, and Slack
(via SlackMCP).

All APIs go through Scalekit's actions.execute_tool(). No direct API imports,
no token management, no credential storage in code.

Tool names and parameter shapes below are verified live against this
workspace's Scalekit environment (env_20324953475777334) at build time, not
guessed -- including a full enumeration of GUSTOMCP's tool catalog via
sk.actions.tools.list_tools(filter=Filter(provider="GUSTOMCP")):

  - GUSTOMCP (connector "gustomcp-SoSOMZ20", identifier "parv@infrasity.com")
    exposes 38 tools total, none of which are time-off/PTO tools. Verified
    by listing every tool name in the catalog: gustomcp_get_employee,
    gustomcp_list_employees, gustomcp_get_contractor, gustomcp_list_contractors,
    gustomcp_get_company, gustomcp_list_departments, gustomcp_list_locations,
    gustomcp_get_time_sheet, gustomcp_list_time_records (hourly time-tracking,
    NOT PTO), gustomcp_get_employee_earnings_summary,
    gustomcp_list_custom_fields_schema, payroll/compensation/contractor tools,
    and so on. There is no gustomcp_get_time_off_balance, no
    gustomcp_list_time_off_requests, no gustomcp_create_time_off_request, and
    no time-off-policy tool of any kind in this connector's real tool
    surface. This was confirmed by enumerating all 38 tool names AND by
    inspecting gustomcp_get_token_info's OAuth scope list, which contains no
    time_off:* scope. See README's Error Handling & Edge Cases section for
    how this agent adapts around that gap.
      -> MCP envelope: {"content": [{"type": "text", "text": "<json-string>"}]}
      -> gustomcp_list_employees returned [] live (this workspace's Gusto
         company, "Infrasity", is tier "contractor_only" per
         gustomcp_get_company, with zero W-2 employees); gustomcp_list_contractors
         returned the one real contractor record for parv@infrasity.com. This
         agent's employee lookup therefore checks employees first, then
         falls back to contractors, and documents both codepaths.
  - GOOGLECALENDAR (connector "googlecalendar", identifier "parv@infrasity.com")
      googlecalendar_create_event(calendar_id=, summary=, start_datetime=
        <RFC3339>, event_duration_hour=, event_duration_minutes=,
        event_type="outOfOffice", transparency="opaque", timezone=)
      googlecalendar_delete_event(calendar_id=, event_id=)
      googlecalendar_list_events(calendar_id=, time_min=, time_max=)
      -> flat dict, no envelope: {"event": {...}} / {"events": [...]}.
      Verified live: there is no end_datetime field on create_event, only
      event_duration_hour/event_duration_minutes -- duration is computed from
      the requested date range. Also verified live: an eventType of
      "outOfOffice" rejects a non-empty `description` field with a real
      Google API 400 (malformedOutOfOfficeEvent), so this connector omits
      description entirely when using that event type.
  - SLACKMCP (connector "slackmcp", identifier "parv@infrasity.com")
      slackmcp_slack_search_users(query=), slackmcp_slack_send_message(
        channel_id=, message=)
      -> MCP envelope, and the unwrapped payload's "results" field is a
      markdown-formatted text block (not structured JSON), matching the
      sibling revenue-forecast-commentary-agent's slack_search_channels
      shape -- verified live with several queries (e.g. query="sid" returned
      "### Result 1 of 1\\nName: Sid\\nUser ID: U09JQLLKKMH\\n...\\nEmail:
      sid@infrasity.com...").
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ConnectorError(Exception):
    """Raised when a connector operation fails and there is no safe fallback."""


def _unwrap_mcp_envelope(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    MCP-based connectors (GUSTOMCP, SLACKMCP) return
    {"content": [{"type": "text", "text": "<json-or-plain-string>"}]} instead
    of a flat payload dict -- verified live against both. GOOGLECALENDAR
    returns the flat payload directly, so this only unwraps when the
    envelope shape is actually present.
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
        parsed = json.loads(text)
        # Gusto's list_* tools return a bare JSON array, not an object --
        # normalize to {"items": [...]} so callers have one shape to check.
        if isinstance(parsed, list):
            return {"items": parsed}
        return parsed
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


class GustoConnector(Connector):
    """
    Gusto API operations -- employee/contractor identity lookup only.

    IMPORTANT LIMITATION (verified live, not assumed): this connector's real
    tool surface has no time-off balance, time-off policy, or time-off
    request tool of any kind. See the module docstring for the full
    enumeration. Every method below is scoped to what GUSTOMCP can actually
    do in this workspace: finding the employee/contractor record to confirm
    they exist in Gusto, and reading company-level context (departments,
    compensation types) for informational purposes. Leave-balance/policy
    validation and time-off submission are handled outside Gusto -- see
    aggregator.py and README's Error Handling section.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "gustomcp-SoSOMZ20"):
        super().__init__(actions, connector_name, identifier)

    def list_employees(self, per: int = 100) -> List[Dict]:
        """List employees (W-2). Returns [] for a contractor-only Gusto company."""
        data = self.execute_tool("gustomcp_list_employees", per=per, page=1) or {}
        return data.get("items") or []

    def list_contractors(self, per: int = 100) -> List[Dict]:
        """List contractors. This workspace's Gusto company is contractor-only."""
        data = self.execute_tool("gustomcp_list_contractors", per=per, page=1) or {}
        return data.get("items") or []

    def get_employee(self, employee_uuid: str) -> Dict:
        """Fetch a single employee's full profile by UUID."""
        return self.execute_tool("gustomcp_get_employee", employee_uuid=employee_uuid) or {}

    def get_contractor(self, contractor_uuid: str) -> Dict:
        """Fetch a single contractor's full profile by UUID."""
        return self.execute_tool("gustomcp_get_contractor", contractor_uuid=contractor_uuid) or {}

    def find_person_by_email(self, email: str) -> Optional[Dict]:
        """
        Find a person (employee or contractor) by email. Checks employees
        first (the common case for most Gusto companies), then falls back to
        contractors, since this workspace's company has zero employees and
        is entirely contractor-based. Returns a dict with an added
        "gusto_person_type" key ("employee" or "contractor"), or None.
        """
        email_lower = email.strip().lower()

        for person in self.list_employees():
            if (person.get("email") or "").strip().lower() == email_lower:
                person["gusto_person_type"] = "employee"
                return person

        for person in self.list_contractors():
            if (person.get("email") or "").strip().lower() == email_lower:
                person["gusto_person_type"] = "contractor"
                return person

        return None

    def get_company(self) -> Dict:
        """Fetch company profile (used to surface Gusto's real compensation/earning types)."""
        return self.execute_tool("gustomcp_get_company") or {}


class GoogleCalendarConnector(Connector):
    """
    Google Calendar API operations -- block the employee's calendar for the
    requested leave dates, and clean it up if a later step fails and the
    whole request needs to be rolled back.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "googlecalendar"):
        super().__init__(actions, connector_name, identifier)

    def create_out_of_office_block(
        self,
        calendar_id: str,
        summary: str,
        start_datetime: str,
        duration_hours: int,
        timezone: str = "UTC",
    ) -> Dict:
        """
        Create an all-day-equivalent "outOfOffice" event spanning the leave
        dates. Google's outOfOffice event type rejects a non-empty
        `description` field (verified live: 400 malformedOutOfOfficeEvent),
        so no description is passed here; the summary alone carries the
        leave-type label.
        """
        return self.execute_tool(
            "googlecalendar_create_event",
            calendar_id=calendar_id,
            summary=summary,
            start_datetime=start_datetime,
            event_duration_hour=duration_hours,
            event_type="outOfOffice",
            transparency="opaque",
            timezone=timezone,
        )

    def delete_event(self, calendar_id: str, event_id: str) -> Dict:
        """Delete a calendar event by ID (used for rollback on partial failure)."""
        return self.execute_tool("googlecalendar_delete_event", calendar_id=calendar_id, event_id=event_id)

    def list_events(self, calendar_id: str, time_min: str, time_max: str) -> List[Dict]:
        """List events in a time window, used to detect an existing overlapping block."""
        data = self.execute_tool(
            "googlecalendar_list_events",
            calendar_id=calendar_id,
            time_min=time_min,
            time_max=time_max,
        ) or {}
        return data.get("events") or []


class SlackConnector(Connector):
    """
    Slack API operations (via the SlackMCP connector) -- resolve the
    manager's email to a Slack user ID and DM them.

    Only the SLACKMCP connector variant is ACTIVE in this workspace (the
    plain SLACK connector is PENDING_AUTH) -- verified live. Send-message
    params are channel_id/message, not channel/text; passing a user ID as
    channel_id sends a DM.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "slackmcp"):
        super().__init__(actions, connector_name, identifier)

    def resolve_user_id(self, email_or_id: str) -> Optional[str]:
        """
        Resolve a manager's email to their Slack user ID via
        slackmcp_slack_search_users. If email_or_id already looks like a raw
        Slack user ID (U...), it's used as-is without a search round-trip.
        """
        if not email_or_id:
            return None

        if email_or_id[:1] == "U" and email_or_id.isalnum():
            return email_or_id

        data = self.execute_tool("slackmcp_slack_search_users", query=email_or_id, limit=10) or {}
        # SlackMCP's search tools return a markdown-formatted "results"
        # string, not structured JSON -- verified live, matching the sibling
        # revenue-forecast-commentary-agent's slack_search_channels shape.
        results_text = data.get("results", "") if isinstance(data, dict) else ""
        return _extract_first_user_id(results_text)

    def send_dm(self, user_id: str, text: str) -> Dict:
        """Send a Slack DM. Passing a user ID as channel_id sends a DM to that user."""
        return self.execute_tool("slackmcp_slack_send_message", channel_id=user_id, message=text)


def _extract_first_user_id(results_text: str) -> Optional[str]:
    """
    Parse a Slack user ID out of SlackMCP's markdown search-results text,
    e.g. "### Result 1 of 1\\nName: Sid\\nUser ID: U09JQLLKKMH\\n...".
    Returns None if no user block is present (e.g. "No results found.").
    """
    match = re.search(r"User ID:\s*([A-Z0-9]+)", results_text or "")
    if match:
        return match.group(1)
    return None
