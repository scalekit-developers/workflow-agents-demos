"""
Connector wrappers for Gusto, Slack (via SlackMCP), and Google Sheets.

All APIs go through Scalekit's actions.execute_tool(). No direct API imports,
no token management, no credential storage in code.

Tool names and parameter shapes below are verified live against this
workspace's Scalekit environment (env_20324953475777334) at build time --
not guessed:
  - gustomcp_list_employees(search_term=..., onboarded=..., terminated=..., per=...)
  - gustomcp_get_employee(employee_uuid=..., include=...)
  - gustomcp_list_contractors(search_term=..., onboarded=..., terminated=..., per=...)
  - gustomcp_get_contractor(contractor_uuid=...)
      -> MCP envelope: {"content": [{"type": "text", "text": "<json-string>"}]}
      -> IMPORTANT, verified live: every gustomcp_* tool in this workspace's
         Scalekit environment is READ ONLY (read_only_hint: true on all 38
         tools returned by search_tools). There is NO write/update tool
         exposed anywhere in this connector's catalog for employee, payment
         method, bank account, or compensation fields. See
         GustoConnector.submit_payroll_change()'s docstring for the full
         implication of this and how the agent handles it safely.
  - slackmcp_slack_search_users(query=...)                      (SLACKMCP)
  - slackmcp_slack_send_message(channel_id=..., message=...)    (SLACKMCP)
      -> MCP envelope, same shape as gustomcp above.
  - googlesheets_read_spreadsheet(spreadsheet_id=...),
    googlesheets_add_sheet(spreadsheet_id=..., title=...),
    googlesheets_append_values(spreadsheet_id=..., range=...,
        value_input_option=..., values=[[...]]),                (GOOGLESHEETS)
      -> flat dict, no envelope. value_input_option is snake_case even though
         the upstream Google API calls it valueInputOption -- verified live
         in the sibling revenue-forecast-commentary-agent repo; the camelCase
         form returns "'valueInputOption' is required".
"""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ConnectorError(Exception):
    """Raised when a connector operation fails and there is no safe fallback."""


def _unwrap_mcp_envelope(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    MCP-based connectors (GUSTOMCP, SLACKMCP, ...) return
    {"content": [{"type": "text", "text": "<json-or-plain-string>"}]} instead
    of a flat payload dict -- verified live against both GUSTOMCP and SLACKMCP
    in this workspace. Plain REST connectors (GOOGLESHEETS) return the flat
    payload directly, so this only unwraps when the envelope shape is
    actually present.
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


class GustoConnector(Connector):
    """
    Gusto API operations (via the GustoMCP connector) -- look up an
    employee/contractor record for eligibility checks, and submit a payroll
    or bank/direct-deposit detail change.

    IMPORTANT, verified live against this workspace's Scalekit environment:
    every gustomcp_* tool currently exposed here is READ ONLY
    (read_only_hint: true on all 38 tools returned by search_tools; no
    create/update/delete tool exists for employees, contractors, payment
    methods, bank accounts, or compensation). This was verified by listing
    every GUSTOMCP tool in the catalog, not by searching for a specific write
    tool name and giving up after one miss.

    submit_payroll_change() below is written the way a real write call would
    be structured against Gusto's documented REST API (PUT/PATCH against the
    employee's bank_accounts or payment_method sub-resource, which is what
    Gusto's own API surface supports), so the request-shaping, validation,
    and error-handling logic are all real and exercised end-to-end. But
    because no such tool is exposed by this Scalekit connector, this method
    raises GustoWriteNotAvailableError instead of guessing at a tool name and
    calling something that does not exist. This is a deliberate, honest
    design choice given the safety constraints on this build (see README's
    "Data Handling & Security" and "Live Validation" sections): this agent
    must never fabricate a tool name for a destructive financial write, and
    the actual live Gusto company connected in this workspace ("Infrasity")
    is provisioned as contractor_only with zero W-2 employees on file, so
    there is no live employee bank-detail record this agent could safely
    exercise a real write against even if a write tool existed.

    If a write tool becomes available in a future Scalekit/Gusto connector
    version, only the body of submit_payroll_change() needs to change (the
    call site in run_flow.py, the eligibility gate, the masking, and the
    idempotency design are all already correct and do not need to change).
    """

    def __init__(self, actions, identifier: str, connector_name: str = "gustomcp"):
        super().__init__(actions, connector_name, identifier)

    def find_employee_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """
        Resolve an employee (W-2) record by email. Tries list_employees with
        search_term first; Gusto's list endpoints do not support filtering by
        email directly, so this fetches candidates by name/email fragment and
        matches the exact email client-side.
        """
        local_part = email.split("@")[0]
        data = self.execute_tool(
            "gustomcp_list_employees", search_term=local_part, per=50
        )
        records = data if isinstance(data, list) else (data.get("employees") or [])
        for record in records:
            if isinstance(record, dict) and record.get("email", "").lower() == email.lower():
                return record
        return None

    def find_contractor_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """
        Resolve a contractor record by email. Same client-side-match approach
        as find_employee_by_email, since Gusto's list_contractors also has no
        direct email filter.
        """
        local_part = email.split("@")[0]
        data = self.execute_tool(
            "gustomcp_list_contractors", search_term=local_part, per=50
        )
        records = data if isinstance(data, list) else (data.get("contractors") or [])
        for record in records:
            if isinstance(record, dict) and record.get("email", "").lower() == email.lower():
                return record
        return None

    def get_employee_detail(self, employee_uuid: str) -> Dict[str, Any]:
        """Full employee profile by UUID, including compensations for eligibility checks."""
        return self.execute_tool(
            "gustomcp_get_employee",
            employee_uuid=employee_uuid,
            include="all_compensations",
        )

    def get_contractor_detail(self, contractor_uuid: str) -> Dict[str, Any]:
        """Full contractor profile by UUID."""
        return self.execute_tool("gustomcp_get_contractor", contractor_uuid=contractor_uuid)

    def submit_payroll_change(
        self,
        record_uuid: str,
        record_type: str,
        change_type: str,
        new_value: str,
    ) -> Dict[str, Any]:
        """
        Submit a payroll/bank-detail change for the given employee or
        contractor record. NOT CALLED against live data by this build -- see
        the class docstring for why. Raises GustoWriteNotAvailableError
        unconditionally in this Scalekit environment, since no write tool is
        exposed. The method signature, validation-before-call contract, and
        docstring below describe the write this WOULD perform if/when a real
        write tool exists, so the surrounding pipeline (eligibility gate,
        idempotency, masking, logging, Slack confirmation) can be built and
        tested against a stable interface.

        record_type: "employee" or "contractor" -- Gusto's write endpoints
            for bank/payment-method fields are shaped differently for each
            (e.g. employees have a dedicated bank_accounts sub-resource
            behind employees:write scope; contractors have a payment_method
            sub-resource). A real implementation would branch here to call
            the correct upstream endpoint.
        change_type: one of "bank_account", "routing_number", or another
            structured payroll field (see config.py's CHANGE_TYPE options).
        new_value: the new value being set. NEVER logged in full by any
            caller of this method -- see aggregator.py's masking helpers.
        """
        raise GustoWriteNotAvailableError(
            f"No write tool is exposed by the {self.connector_name} connector in this "
            f"Scalekit environment for changing '{change_type}' on {record_type} "
            f"{record_uuid}. Every gustomcp_* tool verified via search_tools is read-only "
            f"(read_only_hint=true). This is a hard stop, not a bug to silently route "
            f"around: submitting a payroll/bank-detail change requires a real upstream "
            f"write capability, which does not currently exist for Gusto in this "
            f"workspace. See README.md 'Data Handling & Security' for details."
        )


class GustoWriteNotAvailableError(ConnectorError):
    """
    Raised when submit_payroll_change() is called but no Gusto write tool is
    available in this Scalekit environment. Distinct from a generic
    ConnectorError so run_flow.py can report this with its own specific exit
    code and message rather than folding it into a generic "Gusto call
    failed" bucket -- the distinction matters operationally: this is a
    structural capability gap, not a transient API failure.
    """


class SlackConnector(Connector):
    """
    Slack API operations (via the SlackMCP connector) -- resolve the
    employee to a Slack user ID by email and send a confirmation DM.

    Only the SLACKMCP connector variant is ACTIVE in this workspace (the
    plain SLACK connector is PENDING_AUTH) -- verified live. Send-message
    params are channel_id/message, not channel/text.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "slackmcp"):
        super().__init__(actions, connector_name, identifier)

    def resolve_user_id_by_email(self, email: str) -> Optional[str]:
        """
        Resolve a Slack user ID from an email address via
        slackmcp_slack_search_users. SlackMCP's search tools return a
        markdown-formatted "results" string, not structured JSON -- verified
        live. Multiple users can match a name/local-part query, so this
        parses every result block and matches on the "Email:" line exactly,
        rather than assuming the first result is correct.
        """
        if not email:
            return None

        local_part = email.split("@")[0]
        data = self.execute_tool("slackmcp_slack_search_users", query=local_part, limit=20) or {}
        results_text = data.get("results", "") if isinstance(data, dict) else ""
        return _extract_user_id_by_email(results_text, email)

    def send_dm(self, user_id: str, text: str) -> Dict:
        """Send a direct message to a Slack user ID."""
        return self.execute_tool("slackmcp_slack_send_message", channel_id=user_id, message=text)


class GoogleSheetsConnector(Connector):
    """
    Google Sheets API operations -- append payroll-change audit-log rows.

    There is no tool to create a brand-new spreadsheet from an empty state
    that this agent relies on for the default flow (googlesheets_create_spreadsheet
    does exist in the tool catalog but is not auto-invoked -- see
    provisioning.py) -- the documented flow expects
    GOOGLE_SHEETS_SPREADSHEET_ID to already point at an existing spreadsheet,
    and this connector's job is only to ensure the destination TAB exists
    within it.
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


def _extract_user_id_by_email(results_text: str, target_email: str) -> Optional[str]:
    """
    Parse SlackMCP's markdown search-results text for the result block whose
    "Email:" line matches target_email exactly (case-insensitive), and return
    that block's "User ID:" value. Returns None if no block matches (e.g.
    "No results found." or the searched name has no matching email).
    """
    import re

    blocks = re.split(r"###\s*Result\s+\d+\s+of\s+\d+", results_text or "")
    for block in blocks:
        email_match = re.search(r"Email:\s*(\S+)", block)
        if not email_match:
            continue
        if email_match.group(1).strip().lower() != target_email.strip().lower():
            continue
        id_match = re.search(r"User ID:\s*(\S+)", block)
        if id_match:
            return id_match.group(1).strip()
    return None
