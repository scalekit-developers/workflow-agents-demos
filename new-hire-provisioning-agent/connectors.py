"""
Connector wrappers for Gusto, Google Workspace (via Domain-Wide Delegation),
Notion, and Slack.

All APIs go through Scalekit's actions.execute_tool(). No direct API imports,
no token management, no credential storage in code.

Tool names and parameter shapes below are verified LIVE against this
workspace's Scalekit environment (env_20324953475777334) at build time --
not guessed:

  - gustomcp_list_employees(page=, per=, search_term=, sort_by=, onboarded=,
        onboarded_active=, terminated=, location_uuid=, payroll_uuid=,
        uuids=, include=)                                      (GUSTOMCP,
        connector "gustomcp-SoSOMZ20")
      -> MCP envelope: {"content": [{"type": "text", "text": "<json-array-string>"}]}
      Verified live: called with {"per": 10, "page": 1} against the connected
      sandbox company and received a real (empty) JSON array back. The
      connector's OAuth token later expired mid-build (status flipped from
      ACTIVE to EXPIRED between calls) -- confirming this is a real,
      observed failure mode this agent must handle gracefully (see
      Connector.check_auth and GustoConnector below), not a hypothetical one.
  - gustomcp_get_employee(employee_uuid=, include=)              (GUSTOMCP)
      -> Confirmed via Scalekit's tool catalog (search_tools): employee_uuid
         is required; calling with a syntactically-valid-but-nonexistent UUID
         returned "Resource not found" (not "missing required parameter"),
         confirming the exact parameter name live. include accepts a
         comma-separated list: all_compensations, all_home_addresses,
         company_name, current_home_address, custom_fields, portal_invitations.
      -> MCP envelope, same shape as list_employees.
  - notionmcp_notion-create-pages(parent={"type": "page_id", "page_id": ...},
        pages=[{"properties": {"title": ...}, "content": "<markdown>"}])
                                                                  (NOTIONMCP,
        connector "notionmcp-chAb8Lfz")
      -> Verified live: created a real test page
         (id 3ad26e34-1074-81f2-826d-c561e8a485f0) under an existing parent
         page in this workspace. Note "parent" is a TOP-LEVEL kwarg alongside
         "pages", not nested inside each page dict -- confirmed by both a
         live 400 (parent nested inside a page dict -> "Unrecognized key:
         parent") and a live success (parent top-level). properties.title is
         a plain string, content is markdown text, matching the shape
         reused from performance-review-collector-agent's NotionConnector.
  - notionmcp_notion-search(query=)                               (NOTIONMCP)
      -> Verified live. query must be a non-empty string (empty string
         returns a 400 "too_small" validation error) -- pass at least one
         character.
  - slackmcp_slack_search_channels(query=),
    slackmcp_slack_send_message(channel_id=, message=)            (SLACKMCP,
        connector "slackmcp")
      -> Verified live: search_channels("dev") returned a real channel
         (#bugs-devops-project, C0AKYEQ11L6) with a markdown "results" string
         (not structured JSON), and send_message posted a real Slack message
         to that channel, confirmed by a returned message_link/message_ts.

  - Google Workspace (DWD): the GOOGLEDWD connector shows setup: not_configured
    in this workspace and has ZERO connected accounts and ZERO discoverable
    tools via Scalekit's tool catalog (search_tools with connector=GOOGLEDWD
    and multiple Admin-SDK-style queries, e.g. "create user", "users insert
    directory", returned 0 GOOGLEDWD tool results every time -- the catalog
    genuinely has nothing registered for this connector in this workspace
    yet). get_or_create_connected_account("googledwd", ...) returns
    RESOURCE_NOT_FOUND live, confirmed. Per the explicit instruction not to
    guess/hallucinate tool names for an unverifiable connector, the method
    below is built with a clear signature and TODO docstring instead of a
    fabricated tool name. See README "Google Workspace Provisioning" section
    for the exact setup this requires before this method can be implemented
    for real.
"""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ConnectorError(Exception):
    """Raised when a connector operation fails and there is no safe fallback."""


class ConnectorUnavailableError(ConnectorError):
    """
    Raised when a connector has no connected account at all in this workspace
    (Scalekit's RESOURCE_NOT_FOUND), as opposed to an existing-but-broken
    connection (EXPIRED/PENDING_AUTH/INACTIVE). Distinguished from the base
    ConnectorError so callers (namely Step 2's Google Workspace handling) can
    tell "never configured" apart from "configured but currently failing"
    in their log messages, without needing to parse error text.
    """


def _unwrap_mcp_envelope(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    MCP-based connectors (GUSTOMCP, NOTIONMCP, SLACKMCP, ...) return
    {"content": [{"type": "text", "text": "<json-or-plain-string>"}]} instead
    of a flat payload dict -- verified live against all three. If the decoded
    JSON is a bare list (e.g. gustomcp_list_employees's response), it's
    wrapped as {"items": [...]} so callers always get a dict back.
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
    except (TypeError, ValueError):
        return {"text": text}

    if isinstance(parsed, list):
        return {"items": parsed}
    if isinstance(parsed, dict):
        return parsed
    return {"value": parsed}


class Connector:
    """Base connector class -- shared auth-check and tool-execution logic."""

    def __init__(self, actions, connector_name: str, identifier: str):
        self.actions = actions
        self.connector_name = connector_name
        self.identifier = identifier

    def check_auth(self) -> bool:
        """
        Check if connector is authorized. Returns True if ACTIVE.

        Distinguishes two failure shapes seen live in this workspace:
          - The connection exists but isn't ACTIVE (EXPIRED, PENDING_AUTH,
            INACTIVE, ...): logged with the real status and an authorization
            link when available.
          - The connection doesn't exist at all (RESOURCE_NOT_FOUND, the
            live-verified behavior for an unconfigured connector like
            GOOGLEDWD in this workspace): logged as "not configured" rather
            than a generic error, since that's a distinct and expected state
            for an optional connector, not a broken one.
        Never raises -- a connector being down is reported, not fatal, for
        the Step 0 auth check as a whole.
        """
        try:
            resp = self.actions.get_or_create_connected_account(
                connection_name=self.connector_name,
                identifier=self.identifier,
            )
            status = resp.connected_account.status
        except Exception as e:
            if _is_not_found_error(e):
                logger.warning(
                    f"{self.connector_name} ({self.identifier}) -- NOT CONFIGURED "
                    f"(no connection exists in this Scalekit workspace yet)"
                )
            else:
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
            if _is_not_found_error(e):
                raise ConnectorUnavailableError(f"{tool_name}: {self.connector_name} is not configured") from e
            logger.error(f"Tool execution failed: {tool_name}: {e}")
            raise ConnectorError(f"{tool_name} failed: {e}") from e


def _is_not_found_error(e: Exception) -> bool:
    """True if this exception is Scalekit's RESOURCE_NOT_FOUND (connector never configured)."""
    text = str(e)
    return "RESOURCE_NOT_FOUND" in text or type(e).__name__ == "ScalekitNotFoundException"


class GustoConnector(Connector):
    """
    Gusto API operations (via the GustoMCP connector) -- list employees to
    detect new hires, and fetch a single employee's full profile.

    connector_name must match the exact connection name shown in the
    Scalekit dashboard (e.g. "gustomcp-SoSOMZ20"), not the generic "GUSTOMCP"
    provider label -- Scalekit auto-suffixes connection names per workspace.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "gustomcp-SoSOMZ20"):
        super().__init__(actions, connector_name, identifier)

    def list_employees(
        self,
        onboarded: Optional[bool] = None,
        terminated: Optional[bool] = None,
        page: int = 1,
        per: int = 100,
    ) -> List[Dict]:
        """
        Fetch one page of employees for the connected Gusto company, newest
        first. sort_by="created_at:desc" is passed so a scan naturally checks
        the most-recently-created records first, which matters once a company
        has more than one page of employees.
        """
        kwargs: Dict[str, Any] = {"page": page, "per": per, "sort_by": "created_at:desc"}
        if onboarded is not None:
            kwargs["onboarded"] = onboarded
        if terminated is not None:
            kwargs["terminated"] = terminated

        data = self.execute_tool("gustomcp_list_employees", **kwargs) or {}
        return data.get("items") or data.get("employees") or []

    def get_employee(self, employee_uuid: str) -> Dict:
        """
        Fetch full profile for a single employee: name, hire date, job,
        location. include=company_name is requested so the onboarding doc
        and Slack message can reference the employer name without a second
        round-trip.
        """
        data = self.execute_tool(
            "gustomcp_get_employee",
            employee_uuid=employee_uuid,
            include="company_name",
        ) or {}
        return data

    def find_new_hires(
        self,
        lookback_days: int,
        lookahead_days: int,
        max_pages: int = 5,
        page_size: int = 100,
    ) -> List[Dict]:
        """
        Scan employees (newest-created first) and return those whose
        onboarding is not yet complete OR whose start date falls within the
        [today - lookback_days, today + lookahead_days] window. This is a
        heuristic for "looks like a new hire record", not a literal Gusto
        field: Gusto's onboarded flag reflects self-service paperwork
        completion, which can lag or lead the actual start date, so both
        signals are checked and either one qualifies a record.

        Only NOT-terminated employees are considered (terminated=False),
        since a re-scan should never resurface someone who left before ever
        being provisioned.

        Stops paginating after max_pages (a generous default -- 500
        employees at the default page_size) as a safety bound against
        scanning an entire large company's history every run; combined with
        newest-first sorting, real new hires are always in the earliest
        pages anyway.
        """
        import datetime

        today = datetime.date.today()
        window_start = today - datetime.timedelta(days=lookback_days)
        window_end = today + datetime.timedelta(days=lookahead_days)

        candidates = []
        for page in range(1, max_pages + 1):
            batch = self.list_employees(terminated=False, page=page, per=page_size)
            if not batch:
                break

            for employee in batch:
                if _looks_like_new_hire(employee, window_start, window_end):
                    candidates.append(employee)

            if len(batch) < page_size:
                break  # last page

        return candidates


def _looks_like_new_hire(employee: Dict, window_start, window_end) -> bool:
    """
    True if `employee` (a summary record from gustomcp_list_employees) looks
    like a new hire: onboarding not yet complete, or a start_date within the
    configured lookback/lookahead window. Missing/unparseable start_date
    falls back to onboarding status alone rather than excluding the record,
    since a record worth provisioning shouldn't be silently dropped just
    because one field is absent.
    """
    import datetime

    onboarded = employee.get("onboarded")
    if onboarded is False:
        return True

    start_date_raw = employee.get("start_date") or employee.get("hire_date") or employee.get("jobs", [{}])[0].get("hire_date") if employee.get("jobs") else None
    if not start_date_raw:
        return bool(onboarded is False)

    try:
        start_date = datetime.date.fromisoformat(str(start_date_raw)[:10])
    except ValueError:
        return bool(onboarded is False)

    return window_start <= start_date <= window_end


class GoogleWorkspaceConnector(Connector):
    """
    Google Workspace provisioning via Domain-Wide Delegation (the GOOGLEDWD
    connector in Scalekit's catalog: "Connect to Google Workspace APIs
    (Gmail, Drive, Docs, Sheets, Slides, Forms) using a GCP service account
    with Domain-Wide Delegation for server-to-server authentication without
    user login.").

    STATUS IN THIS WORKSPACE (verified live at build time): GOOGLEDWD shows
    setup: not_configured, has zero connected accounts, and exposes zero
    discoverable tools via Scalekit's search_tools (tried multiple Admin-SDK-
    style queries -- "create user", "users insert directory" -- and got 0
    GOOGLEDWD results every time, meaning this isn't a search-relevance
    problem, the catalog has nothing registered for it here). This means the
    real Scalekit tool name for "create a Google Workspace user" cannot be
    verified in this environment right now.

    TOOL NAME TBD: the method below intentionally does NOT call
    actions.execute_tool() with a guessed tool name. Once GOOGLEDWD is
    connected (see README Prerequisites), verify the real tool name with
    Scalekit's search_tools(connector="GOOGLEDWD") or search_connectors, then
    replace the NotImplementedError below with the real execute_tool() call.
    As a documented reference point only (NOT a confirmed Scalekit tool
    name): Google's own Admin SDK Directory API exposes a REST
    `users.insert` operation for creating a user, which is the underlying
    operation this method is expected to wrap once the real Scalekit tool
    name is known.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "googledwd"):
        super().__init__(actions, connector_name, identifier)

    def provision_user(
        self,
        primary_email: str,
        first_name: str,
        last_name: str,
        recovery_email: Optional[str] = None,
    ) -> Dict:
        """
        Create a Google Workspace user account for a new hire.

        NOT YET IMPLEMENTED: see class docstring. Raises NotImplementedError
        unconditionally so a caller can never silently believe an account was
        created when it wasn't -- run_flow.py's Step 2 catches this
        specifically and logs a clear "Workspace: SKIPPED" outcome rather
        than treating it as an unexpected crash.
        """
        raise NotImplementedError(
            "GoogleWorkspaceConnector.provision_user() is not implemented: the "
            "real Scalekit tool name for Google Workspace user creation via "
            "GOOGLEDWD could not be verified in this workspace (connector is "
            "not_configured, zero connected accounts, zero discoverable tools). "
            "Connect GOOGLEDWD in the Scalekit dashboard (requires a GCP "
            "service account with Domain-Wide Delegation, see README "
            "Prerequisites), verify the real tool name via search_tools, then "
            "implement this method against the confirmed tool name/shape."
        )


class NotionConnector(Connector):
    """
    Notion API operations (via the NotionMCP connector) -- create a new-hire
    onboarding page under a parent/hub page.

    Tool shapes verified live in this build (see module docstring) and reused
    from performance-review-collector-agent's NotionConnector, which
    established the same notionmcp_notion-create-pages/-search shapes
    against this same NOTIONMCP connection.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "notionmcp-chAb8Lfz"):
        super().__init__(actions, connector_name, identifier)

    def verify_parent_page(self, parent_page_id: str) -> bool:
        """
        Best-effort check that `parent_page_id` is reachable by the connected
        integration, by attempting notionmcp_notion-search and checking the
        call itself doesn't error. Notion's search API doesn't offer a
        direct "fetch page by ID" existence check through this tool, so this
        is a smoke test of connectivity/auth rather than a guarantee the ID
        is a real page; provisioning.py's create-then-verify path is the
        stronger check (see provisioning.py).
        """
        try:
            self.execute_tool("notionmcp_notion-search", query=parent_page_id)
            return True
        except ConnectorError:
            return False

    def find_existing_child_page(self, title: str) -> Optional[str]:
        """Search for a page with this exact title. Returns page ID or None."""
        data = self.execute_tool("notionmcp_notion-search", query=title) or {}
        for result in data.get("results") or []:
            if _extract_page_title(result) == title:
                return result.get("id")
        return None

    def create_onboarding_page(self, parent_page_id: str, title: str, markdown_body: str) -> Dict:
        """
        Create a new onboarding page under parent_page_id with the given
        title and markdown content. `parent` is a top-level kwarg (verified
        live), not nested inside each page dict.
        """
        return self.execute_tool(
            "notionmcp_notion-create-pages",
            parent={"type": "page_id", "page_id": parent_page_id},
            pages=[{"properties": {"title": title}, "content": markdown_body}],
        )

    def upsert_onboarding_page(self, parent_page_id: str, title: str, markdown_body: str) -> Dict:
        """
        Create the new hire's onboarding page if one with this exact title
        doesn't already exist under the parent, otherwise return the
        existing page's info without creating a duplicate. Primary duplicate
        protection is state.py's provisioned-employee-ID guard in
        run_flow.py; this title-search is a secondary safety net in case
        state was reset or lost.
        """
        existing_id = self.find_existing_child_page(title)
        if existing_id:
            logger.info(f"Notion page '{title}' already exists ({existing_id}), not creating a duplicate")
            return {"pages": [{"id": existing_id, "properties": {"title": title}}], "already_existed": True}

        logger.info(f"Creating new Notion onboarding page for '{title}'")
        result = self.create_onboarding_page(parent_page_id, title, markdown_body)
        if isinstance(result, dict):
            result["already_existed"] = False
        return result


class SlackConnector(Connector):
    """
    Slack API operations (via the SlackMCP connector) -- resolve a channel
    name to an ID and post a welcome message to it (not a DM).

    Only the SLACKMCP connector variant is ACTIVE in this workspace (the
    plain SLACK connector sits in PENDING_AUTH) -- verified live. Send-message
    params are channel_id/message, not channel/text.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "slackmcp"):
        super().__init__(actions, connector_name, identifier)

    def resolve_channel_id(self, channel_name_or_id: str) -> Optional[str]:
        """
        Resolve a channel like "#general" to its Slack channel ID. If
        channel_name_or_id already looks like a raw ID (C..., G...), it's
        used as-is without a search round-trip.
        """
        if not channel_name_or_id:
            return None

        stripped = channel_name_or_id.lstrip("#")
        if channel_name_or_id[:1] in ("C", "G") and channel_name_or_id.isalnum():
            return channel_name_or_id

        data = self.execute_tool("slackmcp_slack_search_channels", query=stripped) or {}
        # SlackMCP's search tools return a markdown-formatted "results" string,
        # not structured JSON -- verified live. Parse the "Permalink" archive
        # URL's channel ID segment out of the first matching result block.
        results_text = data.get("results", "") if isinstance(data, dict) else ""
        return _extract_first_channel_id(results_text)

    def send_welcome_message(self, channel_id: str, text: str) -> Dict:
        """Post the new hire's welcome message to a channel (channel_id, not a user ID -- this posts publicly, not a DM)."""
        return self.execute_tool("slackmcp_slack_send_message", channel_id=channel_id, message=text)


def _extract_page_title(page: Dict) -> str:
    """Pull the plain-text title out of a Notion page/search-result object."""
    props = page.get("properties")
    if isinstance(props, dict):
        title = props.get("title")
        if isinstance(title, str):
            return title
    # notionmcp_notion-search's result objects use a flat "title" string too.
    title = page.get("title")
    return title if isinstance(title, str) else ""


def _extract_first_channel_id(results_text: str) -> Optional[str]:
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
