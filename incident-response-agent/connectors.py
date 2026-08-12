"""
Connector wrappers for PagerDuty, Jira, Confluence (all plain REST connectors),
and Slack (via SlackMCP).

All APIs go through Scalekit's actions.execute_tool(). No direct API imports,
no token management, no credential storage in code.

Tool names and parameter shapes below are verified LIVE against this
workspace's Scalekit environment (env_20324953475777334) at build time --
not guessed. None of the four connectors had a connected account in this
workspace as of this build (search_connectors reported setup: not_configured
for PAGERDUTY, JIRA, CONFLUENCE, and SLACKMCP), but their tool catalogs are
fully discoverable and their input schemas fully inspectable via Scalekit's
tool catalog search even with zero connected accounts -- the same technique
used for GONG in the competitive-intelligence-briefing-agent sibling repo.

  - pagerduty_incident_create(title=, service_id=, from_email=, urgency=,
        incident_key=, body_details=, escalation_policy_id=, priority_id=)
      REST POST /incidents (base_url https://api.pagerduty.com), OAuth,
      requires the PagerDuty API version header (Accept:
      application/vnd.pagerduty+json;version=2) and a From header carrying
      from_email -- PagerDuty's API requires the creating user's email on
      every write. incident_key, if provided, is PagerDuty's own
      deduplication key: creating a second incident with the same key
      updates/merges into the existing one rather than creating a duplicate,
      which is what this agent uses for its own idempotency (see state.py).
  - pagerduty_services_list(query=, team_ids=, include=, limit=, offset=,
        sort_by=)
      REST GET /services. Used to resolve a configured service NAME to the
      service_id pagerduty_incident_create requires, since the create tool
      only accepts an ID.
  - pagerduty_incident_note_create(id=, content=, from_email=)
      REST POST /incidents/{id}/notes. Used to post the Jira ticket link
      back onto the PagerDuty incident once it exists, so a responder
      looking at PagerDuty can jump straight to the tracking ticket.
  - pagerduty_incidents_list(...), pagerduty_oncalls_list(...),
        pagerduty_escalation_policies_list(...)
      Read-only listing tools, verified but not required for the core flow;
      available for future extension (e.g. resolving "who is on call" by
      name instead of relying on PagerDuty's own escalation routing).

  - jira_issue_create(project_key=, summary=, issue_type=, description=,
        priority_name=, labels=, components=, assignee_account_id=,
        parent_key=, fix_versions=)
      REST POST (base_url https://api.atlassian.com, path under
      /ex/jira/{cloud_id}/rest/api/3/issue), OAuth. Requires project_key,
      summary, and issue_type; issue_type and priority_name are NAMES
      (e.g. "Bug", "High"), not IDs -- these vary per Jira site, so a
      wrong name fails at Jira's API, not Scalekit's, with a message this
      agent surfaces as-is rather than trying to guess a correction.
      Verified: no direct "postmortem link" or "PagerDuty incident ID"
      field exists on create, so this agent puts that context into the
      plain-text description instead of a custom field.

  - confluence_page_create(spaceId=, title=, status=, parentId=,
        body_representation=, body_value=)
      REST POST /wiki/api/v2/pages (base_url https://api.atlassian.com),
      OAuth. Requires a NUMERIC spaceId, not a space key -- verified live
      that the plain CONFLUENCE connector's tool catalog has no spaces-list
      tool to resolve a key to an ID at runtime (only the separate
      ATLASSIANMCP provider's atlassianmcp_getconfluencespaces exposes
      that), so CONFLUENCE_SPACE_ID must be configured directly, the same
      pattern as NOTION_BATTLECARDS_PARENT_PAGE_ID in the
      competitive-intelligence-briefing-agent sibling repo. body_representation
      and body_value must both be provided together or both omitted; this
      agent uses "storage" (Confluence's XHTML-like markup format).

  - slackmcp_slack_send_message(channel_id=, message=)
      Verified in the sibling repos: NOT channel/text (that's the separate,
      often-unauthorized plain SLACK connector). Passing a user ID as
      channel_id opens/posts to that user's DM channel; a channel ID posts
      to that channel.
  - slackmcp_slack_search_channels(query=)
      Verified live in a sibling repo: returns structured JSON (not the
      markdown-text shape slackmcp_slack_search_users returns), with a
      "channels" list of {id, name, ...} -- used to resolve a bare channel
      name from SLACK_CHANNEL to the channel ID slack_send_message needs.

All PagerDuty/Jira/Confluence tools are plain REST (OAuth), unlike Slack's
MCP envelope. See _unwrap_mcp_envelope below for why only Slack responses
need unwrapping here.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ConnectorError(Exception):
    """Raised when a connector operation fails and there is no safe fallback."""


class ConnectorUnavailableError(ConnectorError):
    """
    Raised when a connector has no connected account at all in this workspace
    (Scalekit's RESOURCE_NOT_FOUND), as opposed to an existing-but-broken
    connection (EXPIRED/PENDING_AUTH/INACTIVE). Distinguished from the base
    ConnectorError so callers can tell "never configured" apart from
    "configured but currently failing" in their log messages and exit-code
    handling, without parsing error text.
    """


def _unwrap_mcp_envelope(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    MCP-based connectors (SLACKMCP) return
    {"content": [{"type": "text", "text": "<json-or-plain-string>"}]} instead
    of a flat payload dict. Plain REST connectors (PAGERDUTY, JIRA,
    CONFLUENCE) return the flat payload directly, so this only unwraps when
    the envelope shape is actually present. If the decoded JSON is a bare
    list, it's wrapped as {"items": [...]} so callers always get a dict back.
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

        Distinguishes two failure shapes:
          - The connection exists but isn't ACTIVE (EXPIRED, PENDING_AUTH,
            INACTIVE, ...): logged with the real status and an authorization
            link when available.
          - The connection doesn't exist at all (RESOURCE_NOT_FOUND): logged
            as "not configured" rather than a generic error, since that's a
            distinct and expected state for a not-yet-set-up connector.
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
        single identifier can be connected to multiple connectors of the
        same provider type in one workspace, which makes tool_name-based
        resolution ambiguous (INVALID_ARGUMENT: "multiple connected accounts
        found").
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


class PagerDutyConnector(Connector):
    """
    PagerDuty operations -- resolve a service, trigger an incident, and post
    a follow-up note once the tracking ticket exists.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "PAGERDUTY"):
        super().__init__(actions, connector_name, identifier)

    def resolve_service_id(self, service_id: str, service_name: str) -> str:
        """
        Return a usable PagerDuty service_id: the configured ID directly if
        given (no lookup needed), otherwise resolve service_name via
        pagerduty_services_list. Raises ConnectorError with a specific,
        actionable message if the name matches zero or more than one
        service -- ambiguity here means the wrong service could get paged,
        which is worse than failing loudly.
        """
        if service_id:
            return service_id

        data = self.execute_tool("pagerduty_services_list", query=service_name, limit=25) or {}
        services = data.get("services") or data.get("items") or []
        matches = [s for s in services if isinstance(s, dict) and s.get("name", "").strip().lower() == service_name.strip().lower()]

        if not matches:
            raise ConnectorError(
                f"No PagerDuty service found named '{service_name}'. Set PAGERDUTY_SERVICE_ID "
                f"directly, or confirm the exact service name in the PagerDuty dashboard."
            )
        if len(matches) > 1:
            ids = ", ".join(s.get("id", "?") for s in matches)
            raise ConnectorError(
                f"Multiple PagerDuty services named '{service_name}' found (ids: {ids}). "
                f"Set PAGERDUTY_SERVICE_ID directly to disambiguate."
            )
        return matches[0]["id"]

    def create_incident(
        self,
        title: str,
        service_id: str,
        from_email: str,
        incident_key: Optional[str] = None,
        urgency: Optional[str] = None,
        body_details: Optional[str] = None,
    ) -> Dict:
        """
        Trigger a PagerDuty incident. incident_key, when provided, is
        PagerDuty's own server-side deduplication key: a second call with
        the same key updates the existing incident instead of creating a
        duplicate page, which this agent relies on for its idempotency
        guarantee (see state.py) rather than only tracking state locally.
        """
        kwargs: Dict[str, Any] = {"title": title, "service_id": service_id, "from_email": from_email}
        if incident_key:
            kwargs["incident_key"] = incident_key
        if urgency:
            kwargs["urgency"] = urgency
        if body_details:
            kwargs["body_details"] = body_details
        data = self.execute_tool("pagerduty_incident_create", **kwargs) or {}
        return data.get("incident") or data

    def add_note(self, incident_id: str, content: str, from_email: str) -> Dict:
        """Post a note to an existing PagerDuty incident (e.g. linking the Jira ticket)."""
        return self.execute_tool("pagerduty_incident_note_create", id=incident_id, content=content, from_email=from_email)


class JiraConnector(Connector):
    """Jira operations -- create the incident tracking ticket."""

    def __init__(self, actions, identifier: str, connector_name: str = "JIRA"):
        super().__init__(actions, connector_name, identifier)

    def create_issue(
        self,
        project_key: str,
        summary: str,
        issue_type: str,
        description: Optional[str] = None,
        priority_name: Optional[str] = None,
        labels: Optional[List[str]] = None,
    ) -> Dict:
        """
        Create a Jira issue. issue_type and priority_name are project-specific
        NAMES (e.g. "Bug", "Incident", "High"), not IDs -- an invalid name
        for this specific project fails at Jira's own API with a message
        this agent surfaces as-is (see run_flow.py), rather than guessing a
        correction, since valid values genuinely differ per Jira site/project.
        """
        kwargs: Dict[str, Any] = {"project_key": project_key, "summary": summary, "issue_type": issue_type}
        if description:
            kwargs["description"] = description
        if priority_name:
            kwargs["priority_name"] = priority_name
        if labels:
            kwargs["labels"] = labels
        return self.execute_tool("jira_issue_create", **kwargs) or {}


class ConfluenceConnector(Connector):
    """Confluence operations -- create the postmortem doc."""

    def __init__(self, actions, identifier: str, connector_name: str = "CONFLUENCE"):
        super().__init__(actions, connector_name, identifier)

    def create_page(
        self,
        space_id: str,
        title: str,
        body_value: str,
        parent_id: Optional[str] = None,
    ) -> Dict:
        """
        Create a Confluence page in "storage" format (Confluence's XHTML-like
        markup). body_representation and body_value must both be provided
        together or both omitted -- this always provides both.

        Unlike jira_issue_create, this response DOES carry a usable
        human-facing URL: verified live that "_links" includes "base"
        (e.g. https://yoursite.atlassian.net/wiki) and "webui" (a relative
        path, e.g. /spaces/KEY/pages/12345/Title) -- see page_url() below to
        combine them. No separate site-URL config is needed for Confluence
        the way JIRA_SITE_URL is needed for Jira.
        """
        kwargs: Dict[str, Any] = {
            "spaceId": space_id,
            "title": title,
            "body_representation": "storage",
            "body_value": body_value,
        }
        if parent_id:
            kwargs["parentId"] = parent_id
        return self.execute_tool("confluence_page_create", **kwargs) or {}


def page_url(page: Dict) -> str:
    """
    Build a real, clickable URL for a Confluence page from its create/get
    response, using the "_links.base" + "_links.webui" fields -- verified
    live rather than guessed (unlike jira_issue_create's response, which
    only carries the API's cloud UUID and never a usable site URL). Returns
    "" if either field is missing, so callers fall back to referencing the
    page by title/ID rather than showing a broken link.
    """
    links = page.get("_links") if isinstance(page, dict) else None
    if not isinstance(links, dict):
        return ""
    base, webui = links.get("base"), links.get("webui")
    if not base or not webui:
        return ""
    return f"{base.rstrip('/')}{webui}"


class SlackConnector(Connector):
    """
    Slack operations (via the SlackMCP connector) -- resolve a channel name
    to an ID and post the on-call notification.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "SLACKMCP"):
        super().__init__(actions, connector_name, identifier)

    def resolve_channel_id(self, channel_name_or_id: str) -> Optional[str]:
        """
        Resolve a bare channel name (e.g. "on-call") to its Slack channel
        ID. If channel_name_or_id already looks like a raw channel ID (C...
        or G...), it's used as-is without a search round-trip. Returns None
        if no match is found -- callers must not silently skip the
        notification in that case; posting to on-call is not optional the
        way a single rep's DM is in a digest-style agent.

        Verified live against this workspace: slackmcp_slack_search_channels
        returns a "results" field containing a markdown TEXT block (headed
        "# Search Results for: <query>"), not structured JSON with a
        "channels" array -- the same markdown-search-result shape
        slackmcp_slack_search_users returns in the sibling repos. Parsed
        with regex rather than treated as a list of dicts.
        """
        if not channel_name_or_id:
            return None

        if channel_name_or_id[0] in ("C", "G") and channel_name_or_id.isalnum() and len(channel_name_or_id) >= 9:
            return channel_name_or_id

        data = self.execute_tool("slackmcp_slack_search_channels", query=channel_name_or_id) or {}
        results_text = data.get("results", "") if isinstance(data, dict) else ""
        return _extract_channel_id_matching(results_text, channel_name_or_id)

    def send_message(self, channel_id: str, text: str) -> Dict:
        """Post a message to a channel (or DM, if channel_id is a user ID)."""
        return self.execute_tool("slackmcp_slack_send_message", channel_id=channel_id, message=text)


def _extract_channel_id_matching(results_text: str, query: str) -> Optional[str]:
    """
    Parse a Slack channel ID out of SlackMCP's markdown search-results text.
    Verified live against this workspace: blocks are separated by "\\n---\\n",
    each with a "Name: #channel-name" line and a "Permalink: [link](https://
    .../archives/CHANNEL_ID)" line -- unlike slack_search_users's results,
    there is no separate "Channel ID:" field, so the ID must be pulled out
    of the permalink URL's trailing path segment.

    Requires an EXACT name match (case-insensitive, # optional) -- unlike
    _extract_user_id_matching's first-block fallback for fuzzy name queries,
    there is no safe fallback here: posting the on-call notification to the
    wrong channel is a worse outcome than failing to resolve one at all.
    """
    blocks = re.split(r"\n---\n", results_text or "")
    target = query.strip().lstrip("#").lower()

    for block in blocks:
        name_match = re.search(r"^Name:\s*#?(\S+)", block, flags=re.MULTILINE)
        if not name_match or name_match.group(1).strip().lower() != target:
            continue
        permalink_match = re.search(r"Permalink:\s*\[link\]\(https://[^/]+/archives/(\w+)\)", block)
        if permalink_match:
            return permalink_match.group(1)

    return None
