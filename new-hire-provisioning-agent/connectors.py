"""
Connector wrappers for Deel (via DeelMCP), Google Workspace (via Domain-Wide
Delegation), Notion, and Slack.

All APIs go through Scalekit's actions.execute_tool(). No direct API imports,
no token management, no credential storage in code.

Tool names and parameter shapes below are verified LIVE against this
workspace's Scalekit environment (env_20324953475777334):

  - deelmcp_org_direct_employee_create(data={client:{legal_entity:{id=},
        team:{id=}, department:{id=}}, employee:{email=, work_email=,
        country=, state=, first_name=, last_name=, nationality=},
        employment:{type="FULL_TIME"|"PART_TIME", job_title=, seniority=,
        start_date=}, compensation_details:{salary=, currency=, scale=}})
        (DEELMCP, connector "deelmcp-zTWsHKTh")
      -> "Creates a direct employee record under the organization's own
         legal entity, provisioning both a person and an employment
         contract. For onboarding employees managed through your own
         payroll providers." Verified live end-to-end: a real employee
         was created and returned a real id, contract_id, and echoed
         employment/compensation details.
      -> employee.state is REQUIRED for at least India (country=IN) even
         though the schema marks it optional -- confirmed live: omitting it
         returns a real 400 "No state was selected". Likely required for
         other countries with state/province-level tax jurisdictions too;
         not exhaustively tested here.
      -> employment.seniority wants the SENIORITY NAME STRING (e.g. "Mid
         (Individual Contributor Level 2)"), not the numeric ID that
         deelmcp_lookup_seniority_list's own response returns as "id" --
         confirmed live: the numeric ID string ("2") was rejected with a
         real 404 "Seniority not found"; the name string from the same
         lookup response succeeded.
      -> Response is a single object under "data" (not a list).
  - deelmcp_org_legal_entity_list() / deelmcp_org_team_list() /
        deelmcp_org_department_list()                              (DEELMCP)
      -> All three verified live to be genuine no-argument bulk listings
         (unlike deelmcp_hris_org_chart_get, which requires a pre-known
         GUID and cannot be used as a bulk fetch -- see the sibling
         pto-leave-request-agent's connectors.py for that finding). Used to
         resolve DEEL_LEGAL_ENTITY_ID/DEEL_TEAM_ID/DEEL_DEPARTMENT_ID
         automatically at startup instead of requiring them as hardcoded
         config, when exactly one candidate exists; ambiguity (more than
         one legal entity or team) falls back to requiring the env var.
  - deelmcp_lookup_seniority_list()                                 (DEELMCP)
      -> Real, verified live: returns id/name/level triples (e.g.
         {"id": 2, "name": "Mid (Individual Contributor Level 2)",
         "level": 2}). Used to resolve NEW_HIRE_SENIORITY (a short config
         value like "mid") to the real name string org_direct_employee_create
         needs.
  - No delete/terminate/offboard tool exists anywhere in this DEELMCP
    catalog for a direct employee (confirmed live by exhausting every
    plausible query: "direct_employee_delete", "_terminate", "_offboard",
    "contract_terminate", "hris_employee_terminate", "worker_offboard" --
    all zero results). This means a mistakenly-created employee record
    cannot be cleaned up through this agent or any other Scalekit tool;
    see README for why NEW_HIRE_DRY_RUN exists and is recommended for
    first-time setup verification.
  - deelmcp_onboarding_tracker_list(include_overview=True, limit=<=200,
        cursor=, ...)                                              (DEELMCP)
      -> CORRECTION to an earlier finding in this same file: this genuinely
         IS a real "who is currently going through onboarding" listing tool
         -- confirmed live against this workspace's real Deel org, returning
         real records (including every test hire created via
         create_direct_employee above, all showing progress.status:
         "INVITED", plus one real pre-existing employee this agent never
         touched). The earlier "Deel has no list of pending hires" note
         above is still correct in the narrower sense that Deel cannot
         detect a hire before it exists as a real Deel record -- someone
         (an HR admin, or this agent's own create-mode) still has to create
         it first -- but once a record exists, this tool DOES let a caller
         detect and re-derive its status without already knowing its ID.
      -> With include_overview=True, each record carries real, usable
         onboarding content beyond just status: hris_profile.name/work_email
         /email, summary (START_DATE, LOCATION_OF_WORK/country, JOB_TITLE,
         BACKGROUND_CHECK), contract.id/created_at/status, and a
         checklist of pending onboarding steps with due dates. It does NOT
         carry salary/currency/seniority/nationality/state (compensation
         and immigration-adjacent fields Deel keeps elsewhere), which is
         why scan mode (see run_flow.py) only drives Workspace/Notion/Slack
         from this data, never a Deel creation call.
      -> Response is {"data": [...], "page": {"total_rows": int, "cursor":
         str|None}} -- a real cursor-paginated list, not a single object.
      -> The tool's own progressStatuses filter param (enum:
         ACTIVE/INACTIVE/ONBOARDING) does NOT reliably surface INVITED
         records -- confirmed live: progressStatuses=["ONBOARDING"] returned
         ZERO records while an unfiltered call in the same account showed 3
         real records with progress.status: "INVITED" at that moment. See
         list_onboarding_hires() below, which filters client-side on the
         real progress.status field instead of trusting this param.

  - notionmcp_notion-create-pages(parent={"type": "page_id", "page_id": ...},
        pages=[{"properties": {"title": ...}, "content": "<markdown>"}])
                                                                  (NOTIONMCP,
        connector "notionmcp-chAb8Lfz")
      -> Verified live: created a real test page under an existing parent
         page in this workspace. "parent" is a TOP-LEVEL kwarg alongside
         "pages", not nested inside each page dict. properties.title is a
         plain string, content is markdown text.
  - notionmcp_notion-search(query=)                               (NOTIONMCP)
      -> Verified live. query must be a non-empty string (empty string
         returns a 400 "too_small" validation error) -- pass at least one
         character.
  - slackmcp_slack_search_channels(query=),
    slackmcp_slack_send_message(channel_id=, message=)            (SLACKMCP,
        connector "slackmcp")
      -> Verified live: search_channels returned a real channel with a
         markdown "results" string (not structured JSON), and send_message
         posted a real Slack message, confirmed by a returned
         message_link/message_ts.

  - googledwd_create_admin_user(primary_email=, given_name=, family_name=,
        password=(optional), org_unit_path=(optional),
        change_password_at_next_login=(optional))            (GOOGLEDWD,
        connector "googledwd-f0ebCm3b")
      -> REST POST https://admin.googleapis.com/admin/directory/v1/users
         via DWD service account credentials, auth.strategy: BEARER.
         Verified live end-to-end after fixing the connector's Domain-Wide
         Delegation setup (see below): a real Workspace user was created
         and confirmed readable back via googledwd_get_admin_user, then
         removed via googledwd_delete_admin_user for cleanup.
      -> Requires impersonating a Workspace SUPER ADMIN as the connected
         account's identifier -- confirmed live: impersonating a non-admin
         user (even with the DWD connection itself ACTIVE and correctly
         scoped) gets a real 403 "Not Authorized to access this
         resource/api" from Google's Admin Directory API itself, a
         separate authorization layer from OAuth/DWD scope grants. In this
         workspace, contact@infrasity.com is the verified-working Super
         Admin subject; parv@infrasity.com and shan@infrasity.com are not
         Super Admins and get this 403.
      -> DWD auth setup that was required before this worked (verified
         live, GCP service account + Workspace Admin Console): the
         Scalekit connector's own Scopes list (Configuration tab) always
         includes three non-removable baseline scopes -- openid,
         userinfo.email, userinfo.profile -- alongside whichever API
         scopes are checked (e.g. admin.directory.user). Google's DWD
         token endpoint validates the ENTIRE requested scope set as one
         all-or-nothing grant: authorizing only admin.directory.user in
         Workspace Admin Console's Domain-wide Delegation entry (and
         omitting the three baseline scopes) produced a real 401
         "REAUTHENTICATION_NEEDED" / "Google DWD token endpoint returned
         status 401" on every connected-account auth attempt. Adding all
         four scopes (openid, userinfo.email, userinfo.profile,
         admin.directory.user) to the same Client ID's DWD authorization
         resolved it -- connected_account.status flipped from PENDING_AUTH
         to ACTIVE.
"""

import json
import logging
import secrets
import string
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
    MCP-based connectors (DEELMCP, NOTIONMCP, SLACKMCP) return
    {"content": [{"type": "text", "text": "<json-or-plain-string>"}]} instead
    of a flat payload dict -- verified live against all three. If the decoded
    JSON is a bare list, it's wrapped as {"items": [...]} so callers always
    get a dict back.
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


class DeelConnector(Connector):
    """
    Deel API operations -- resolve the org's legal entity/team/department and
    seniority-level catalog, and create a real direct-employee record for a
    new hire.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "deelmcp-zTWsHKTh"):
        super().__init__(actions, connector_name, identifier)

    def list_legal_entities(self) -> List[Dict]:
        data = self.execute_tool("deelmcp_org_legal_entity_list") or {}
        return data.get("data") or data.get("items") or []

    def list_teams(self) -> List[Dict]:
        data = self.execute_tool("deelmcp_org_team_list") or {}
        return data.get("data") or data.get("items") or []

    def list_departments(self) -> List[Dict]:
        data = self.execute_tool("deelmcp_org_department_list") or {}
        return data.get("data") or data.get("items") or []

    def list_onboarding_hires(self, statuses: List[str], limit: int = 100) -> List[Dict]:
        """
        Real, verified live: list workers currently in Deel's onboarding
        tracker. include_overview is always requested since scan mode needs
        the nested hris_profile/summary/checklist fields to build
        Notion/Slack content -- see module docstring.

        `statuses` is applied CLIENT-SIDE against each record's real
        progress.status field, not passed to the tool's own
        progressStatuses filter -- confirmed live that the two do not agree:
        progressStatuses only accepts ACTIVE/INACTIVE/ONBOARDING, but every
        record actually seen in this account's tracker (including newly
        created, not-yet-accepted hires) reports progress.status: "INVITED",
        and filtering server-side by progressStatuses=["ONBOARDING"]
        returned ZERO of those real INVITED records -- verified by cross-
        checking against an unfiltered call showing all 5 real records,
        3 of which are INVITED and were silently excluded by the ONBOARDING
        filter. Server-side progressStatuses is passed through unset
        (Deel's own default) and this method filters the returned records
        against `statuses` itself instead, so callers asking for INVITED
        hires actually get them.

        Only the first page is fetched (a single HR-managed org's in-flight
        onboarding count is expected to be well under `limit`, and
        run_flow.py logs a clear warning if the real total_rows exceeds what
        was fetched rather than silently truncating).
        """
        data = self.execute_tool(
            "deelmcp_onboarding_tracker_list",
            include_overview=True,
            limit=limit,
        ) or {}
        records = data.get("data") or data.get("items") or []
        page = data.get("page") or {}
        total_rows = page.get("total_rows")
        if isinstance(total_rows, int) and total_rows > len(records):
            logger.warning(
                f"deelmcp_onboarding_tracker_list reports {total_rows} total matching records but "
                f"only {len(records)} were fetched (limit={limit}) -- some in-progress hires may not "
                f"be processed this run. Increase the fetch limit or re-run to pick up the rest."
            )

        wanted = set(statuses)
        return [r for r in records if (r.get("progress") or {}).get("status") in wanted]

    def list_seniorities(self) -> List[Dict]:
        """Real predefined seniority levels: {"id": int, "name": str, "level": int}."""
        data = self.execute_tool("deelmcp_lookup_seniority_list") or {}
        return data.get("data") or data.get("items") or []

    def resolve_seniority_name(self, seniority_query: str) -> Optional[str]:
        """
        Resolve a short config value (e.g. "mid", "senior") to the real
        seniority NAME STRING org_direct_employee_create requires -- not the
        numeric ID deelmcp_lookup_seniority_list also returns, which the
        create tool rejects with a real 404 "Seniority not found" (verified
        live). Matches case-insensitively against a substring of the real
        name.
        """
        query_lower = seniority_query.strip().lower()
        for level in self.list_seniorities():
            name = level.get("name", "")
            if query_lower in name.lower():
                return name
        return None

    def create_direct_employee(
        self,
        legal_entity_id: str,
        team_id: str,
        email: str,
        work_email: str,
        country: str,
        first_name: str,
        last_name: str,
        nationality: str,
        job_title: str,
        seniority_name: str,
        start_date: str,
        salary: float,
        currency: str,
        state: Optional[str] = None,
        department_id: Optional[str] = None,
        employment_type: str = "FULL_TIME",
    ) -> Dict:
        """
        Create a real direct-employee record: a person plus an employment
        contract under the organization's own legal entity.

        state is required for at least India (verified live: omitting it
        for country="IN" returns a real 400 "No state was selected") -- pass
        it whenever your target country needs one; Deel's own API is the
        source of truth for which countries require it, not this agent.
        """
        client: Dict[str, Any] = {"legal_entity": {"id": legal_entity_id}, "team": {"id": team_id}}
        if department_id:
            client["department"] = {"id": department_id}

        employee: Dict[str, Any] = {
            "email": email,
            "work_email": work_email,
            "country": country,
            "first_name": first_name,
            "last_name": last_name,
            "nationality": nationality,
        }
        if state:
            employee["state"] = state

        payload = {
            "client": client,
            "employee": employee,
            "employment": {
                "type": employment_type,
                "job_title": job_title,
                "seniority": seniority_name,
                "start_date": start_date,
            },
            "compensation_details": {
                "salary": salary,
                "currency": currency,
            },
        }
        # The tool's own input_schema requires the whole payload wrapped
        # under a single top-level "data" key (confirmed live: unpacking
        # payload's keys as separate top-level kwargs instead produced a
        # real 400 "data" Required error) -- pass it as one data= kwarg,
        # not **payload.
        data = self.execute_tool("deelmcp_org_direct_employee_create", data=payload) or {}
        return data.get("data") or data


class GoogleWorkspaceConnector(Connector):
    """
    Google Workspace provisioning via Domain-Wide Delegation (the GOOGLEDWD
    connector in Scalekit's catalog: "Connect to Google Workspace APIs
    (Gmail, Drive, Docs, Sheets, Slides, Forms) using a GCP service account
    with Domain-Wide Delegation for server-to-server authentication without
    user login.").

    VERIFIED LIVE AND WORKING in this workspace (see module docstring for the
    full DWD scope/admin-privilege setup this required). The identifier
    passed to this connector is the impersonated Workspace subject, and it
    MUST be a Super Admin -- confirmed live via a real 403 for two non-admin
    subjects and a real success for a Super Admin subject. Callers should
    default GOOGLE_WORKSPACE_USER to a confirmed Super Admin, not just any
    HR admin's email.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "googledwd-f0ebCm3b"):
        super().__init__(actions, connector_name, identifier)

    def provision_user(
        self,
        primary_email: str,
        first_name: str,
        last_name: str,
        recovery_email: Optional[str] = None,
    ) -> Dict:
        """
        Create a real Google Workspace user account for a new hire via the
        Admin Directory API (googledwd_create_admin_user).

        recovery_email is accepted for interface symmetry with the other
        provisioning calls but is not part of this tool's real input schema
        (verified live: additionalProperties: false, only primary_email,
        given_name, family_name, org_unit_path, change_password_at_next_login,
        password) -- it is intentionally NOT sent, since Google's own
        create-user endpoint has no equivalent field on this call; recovery
        email is a separate, later self-service setting on the account.

        A real password is REQUIRED here -- confirmed live: omitting it
        entirely produced a real 400 "Invalid Password" from Google's Admin
        Directory API for this domain (this only surfaces once DWD auth
        itself and the Super Admin impersonation are both already correct;
        see module docstring). The tool's own schema documents password as
        optional "unless the domain provisions users via SSO only", but this
        domain is not SSO-only, so a real password is generated here rather
        than assumed optional. change_password_at_next_login=True so the
        generated password is a one-time bootstrap value only -- the new
        hire sets their own real password on first login and this agent
        never logs or stores the generated value beyond this call.
        """
        temp_password = _generate_temp_password()
        data = self.execute_tool(
            "googledwd_create_admin_user",
            primary_email=primary_email,
            given_name=first_name,
            family_name=last_name,
            password=temp_password,
            change_password_at_next_login=True,
        ) or {}
        return data.get("data") or data


def _generate_temp_password(length: int = 20) -> str:
    """
    Generate a cryptographically random bootstrap password meeting Google's
    Admin Directory API password requirements (8-100 characters). Never
    logged: this is a one-time value the new hire immediately replaces via
    change_password_at_next_login, not a credential this agent needs to
    remember or hand off.
    """
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


class NotionConnector(Connector):
    """
    Notion API operations (via the NotionMCP connector) -- create a new-hire
    onboarding page under a parent/hub page.
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
