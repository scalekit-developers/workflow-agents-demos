"""
Connector wrappers for Deel (via DeelMCP), Google Calendar, and Slack
(via SlackMCP).

All APIs go through Scalekit's actions.execute_tool(). No direct API imports,
no token management, no credential storage in code.

Tool names and parameter shapes below are verified live against this
workspace's Scalekit environment (env_20324953475777334):

  - DEELMCP (connector "deelmcp-zTWsHKTh", identifier "parv@infrasity.com")
    has a real, first-class time-off surface -- genuine balance, policy, and
    request tools:
      deelmcp_timeoff_entitlement_list(hris_profile_id=, policy_type_name=)
        -> real remaining balance per policy type.
      deelmcp_timeoff_policy_list(hris_profile_id=, policy_type_name=)
        -> resolves a policy_type_name (e.g. "Vacation") to the specific
           policy_id assigned to this person, needed by request_create.
      deelmcp_timeoff_request_create(data={recipient_profile_id=,
        start_date=, end_date=, policy_id=, description=, status=
        "REQUESTED"|"APPROVED"})
        -> real submission. status="REQUESTED" leaves it pending a
           reviewer's decision (via timeoff_request_review); this agent
           always submits as REQUESTED, never auto-approves its own
           request.
      deelmcp_timeoff_request_list(status=[...], start_date=, end_date=)
        -> used to check for an existing REQUESTED/APPROVED request
           overlapping the same dates before creating a new one.
      deelmcp_lookup_timeoff_type_list()
        -> the platform-wide list of policy type names, used at startup to
           fail fast if PTO_TYPE's mapped Deel policy type name isn't a
           real, recognized type on this platform.
    None of these tools accept an email or return one directly. Every
    time-off tool is scoped by hris_profile_id (a UUID), and there is no
    "find worker by email" tool anywhere in DEELMCP's catalog (confirmed by
    exhausting every plausible query: "worker search", "find worker",
    "employee lookup", none matched).

    deelmcp_hris_org_chart_get() looked like the bulk fetch to match emails
    against, but verified live against a real connected account: it rejects
    every call without a groupByValue, and every grouping strategy
    (WORKER_RELATIONS, COUNTRY, HRIS_TEAMS, TEAM_GROUPS) requires that value
    to already be a specific GUID (e.g. a specific team ID) known in
    advance -- it is not a "give me everyone" bulk fetch the way its
    description implied, so it cannot resolve an unknown employee's email at
    all.

    deelmcp_contract_list(), by contrast, IS a genuine no-argument bulk
    listing tool -- verified live to return every contract in the org with
    no required filters, each including a worker.id field that IS the same
    UUID deelmcp_timeoff_policy_list/entitlement_list/request_create expect
    as hris_profile_id (verified live: a real worker.id from contract_list
    was accepted by timeoff_policy_list and returned that worker's real
    assigned policy). This is the real employee-resolution path this agent
    uses: fetch every contract, match by the contract's associated
    person/worker email client-side. A contract whose worker hasn't
    completed onboarding yet has worker=None (verified live) and is
    correctly skipped rather than treated as a match.
      -> MCP envelope: {"content": [{"type": "text", "text": "<json-string>"}]}
         -- verified live against deelmcp_contract_list and
         deelmcp_timeoff_policy_list/entitlement_list. DEELMCP tool
         responses are not uniformly flat dicts; always check for the
         envelope rather than assuming a shape from one tool's schema.
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
      sibling repos' slack_search_channels/slack_search_users shape --
      verified live with several queries (e.g. query="sid" returned
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
    MCP-based connectors (SLACKMCP) return
    {"content": [{"type": "text", "text": "<json-or-plain-string>"}]} instead
    of a flat payload dict -- verified live. DEELMCP and GOOGLECALENDAR
    return the flat payload directly, so this only unwraps when the
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


class DeelConnector(Connector):
    """
    Deel API operations -- resolve an employee's hris_profile_id, read their
    real time-off balance and assigned policy, and submit a real time-off
    request.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "deelmcp-zTWsHKTh"):
        super().__init__(actions, connector_name, identifier)

    def list_contracts(self) -> List[Dict]:
        """
        Fetch every contract in the organization. Verified live to be a
        genuine no-argument bulk listing (unlike deelmcp_hris_org_chart_get,
        which requires a pre-known GUID and cannot be used as a bulk fetch --
        see the module docstring). Each contract includes a worker.id field
        once the worker has completed onboarding; a contract still in
        progress has worker=None and is skipped by find_person_by_email
        below rather than treated as a match.
        """
        data = self.execute_tool("deelmcp_contract_list") or {}
        return data.get("data") or data.get("items") or []

    def get_person(self, hris_profile_id: str) -> Dict:
        """
        Fetch a person's full HRIS profile, including their real email
        (under data.employments[N].email / work_email -- verified live).
        Used by find_person_by_email below, since contract_list's per-worker
        summary carries no email field of its own.
        """
        data = self.execute_tool("deelmcp_org_hris_person_get", hris_profile_id=hris_profile_id) or {}
        return data.get("data") or data

    def find_person_by_email(self, email: str) -> Optional[Dict]:
        """
        Find a worker by email: list every contract, and for each one with a
        completed-onboarding worker (worker.id present), fetch their full
        HRIS profile and check its employment email(s). There is no
        server-side email filter on any Deel tool in this catalog, so this
        is a real, verified two-call-per-candidate lookup, not a single
        indexed query -- acceptable for the small worker counts typical of
        an org this agent would run against, but a real cost worth knowing
        about for a very large organization (see README).

        Returns a dict with at least "hris_profile_id" set, or None if no
        contract's worker matches the given email.
        """
        email_lower = email.strip().lower()

        for contract in self.list_contracts():
            if not isinstance(contract, dict):
                continue
            worker = contract.get("worker")
            if not isinstance(worker, dict) or not worker.get("id"):
                continue  # contract still in progress, no onboarded worker yet

            hris_profile_id = worker["id"]
            person = self.get_person(hris_profile_id)
            for employment in person.get("employments") or []:
                candidate_email = (employment.get("email") or employment.get("work_email") or "").strip().lower()
                if candidate_email == email_lower:
                    first_name, last_name = _split_employment_name(employment.get("name") or "")
                    return {
                        "hris_profile_id": hris_profile_id,
                        "email": candidate_email,
                        "first_name": first_name,
                        "last_name": last_name,
                    }

        return None

    def get_policy_for_type(self, hris_profile_id: str, policy_type_name: str) -> Optional[Dict]:
        """Resolve the policy assigned to this person for a given Deel policy type name."""
        data = self.execute_tool(
            "deelmcp_timeoff_policy_list", hris_profile_id=hris_profile_id, policy_type_name=policy_type_name
        ) or {}
        policies = data.get("policies") or data.get("items") or []
        return policies[0] if policies else None

    def get_entitlement(self, hris_profile_id: str, policy_type_name: str) -> Optional[Dict]:
        """Fetch the real remaining balance for this person and policy type."""
        data = self.execute_tool(
            "deelmcp_timeoff_entitlement_list", hris_profile_id=hris_profile_id, policy_type_name=policy_type_name
        ) or {}
        entitlements = data.get("entitlements") or data.get("items") or []
        return entitlements[0] if entitlements else None

    def list_requests(
        self, start_date: str, end_date: str, statuses: Optional[List[str]] = None
    ) -> List[Dict]:
        """
        List existing time-off requests overlapping a date range, used to
        detect duplicates. Despite deelmcp_timeoff_request_create accepting
        a bare YYYY-MM-DD date, deelmcp_timeoff_request_list's start_date/
        end_date require a full date-time string -- verified live: a bare
        date was rejected with "Invalid datetime". start_date is normalized
        to midnight and end_date to end-of-day so the requested calendar
        days are fully covered by the query window.
        """
        kwargs: Dict[str, Any] = {
            "start_date": f"{start_date}T00:00:00Z",
            "end_date": f"{end_date}T23:59:59Z",
        }
        if statuses:
            kwargs["status"] = statuses
        data = self.execute_tool("deelmcp_timeoff_request_list", **kwargs) or {}
        return data.get("data") or data.get("items") or []

    def create_request(
        self,
        recipient_profile_id: str,
        start_date: str,
        end_date: str,
        policy_id: str,
        description: str = "",
    ) -> Dict:
        """
        Submit a real time-off request. Always submitted as status=REQUESTED
        (never APPROVED): this agent proposes the request on the employee's
        behalf, it does not also decide on it -- approval is a separate,
        later action (deelmcp_timeoff_request_review) taken by the manager
        or an approver, not by this agent auto-approving its own submission.
        """
        payload: Dict[str, Any] = {
            "recipient_profile_id": recipient_profile_id,
            "start_date": start_date,
            "end_date": end_date,
            "policy_id": policy_id,
            "status": "REQUESTED",
        }
        if description:
            payload["description"] = description
        data = self.execute_tool("deelmcp_timeoff_request_create", data=payload) or {}
        # Verified live: the real response is {"time_offs": [{...}]} -- a
        # list, not the {"data": {...}} shape used by most other DEELMCP
        # write tools (or a bare object). Confirmed against a real created
        # request: the single created record's real "id" only exists inside
        # this list.
        time_offs = data.get("time_offs") or data.get("data") or []
        if isinstance(time_offs, list) and time_offs:
            return time_offs[0]
        if isinstance(time_offs, dict):
            return time_offs
        return data

    def list_policy_types(self) -> List[Dict]:
        """List the platform-wide time-off type catalog, used to validate PTO_TYPE's mapped Deel policy type name at startup."""
        data = self.execute_tool("deelmcp_lookup_timeoff_type_list") or {}
        return data.get("data") or data.get("items") or []


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
        results_text = data.get("results", "") if isinstance(data, dict) else ""
        return _extract_first_user_id(results_text)

    def send_dm(self, user_id: str, text: str) -> Dict:
        """Send a Slack DM. Passing a user ID as channel_id sends a DM to that user."""
        return self.execute_tool("slackmcp_slack_send_message", channel_id=user_id, message=text)


def _split_employment_name(name: str) -> tuple:
    """
    Split a Deel employment's "name" field into (first_name, last_name).

    Verified live: this field is not a clean person name -- it's the
    contract title, formatted as "{First} {Last} - {job_title, or
    'Not specified'}" (e.g. "Sid Lais - Not specified"). A naive
    name.split(" ") would put "Lais - Not specified" entirely into
    last_name. The " - " separator is stripped first so only the actual
    name portion is split, since there is no cleaner first/last name field
    anywhere in deelmcp_org_hris_person_get's real response (confirmed by
    inspecting every top-level and employment-level key live).
    """
    name_part = name.split(" - ", 1)[0].strip() if name else ""
    if not name_part:
        return "", ""
    parts = name_part.split(" ")
    return parts[0], " ".join(parts[1:])


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
