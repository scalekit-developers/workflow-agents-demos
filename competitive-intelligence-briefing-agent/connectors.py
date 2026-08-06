"""
Connector wrappers for Gong, Notion (via NotionMCP), and Slack (via SlackMCP).

All APIs go through Scalekit's actions.execute_tool(). No direct API imports,
no token management, no credential storage in code.

Tool names and parameter shapes below are verified LIVE against this
workspace's Scalekit environment (env_20324953475777334) at build time --
not guessed:

  - notionmcp_notion-search(query=)                                (NOTIONMCP,
        connector "notionmcp-chAb8Lfz")
      -> Verified live: MCP envelope {"content": [{"type": "text", "text":
         "<json-string>"}]}, decoded JSON shape {"results": [{"id":...,
         "title":..., "url":..., "type": "page", ...}], "type":
         "workspace_search"}. Also verified live: the search index is
         EVENTUALLY CONSISTENT, not immediate -- a page created moments
         earlier returned zero results from notion-search, then was found by
         the same query a few minutes later. Because of this, battlecard
         lookup below uses notion-fetch on the known parent page as the
         PRIMARY method (immediate, does not depend on index freshness), and
         only falls back to notion-search as a secondary signal.
  - notionmcp_notion-fetch(id=)                                    (NOTIONMCP)
      -> Verified live against a real parent page: returns {"metadata": {...},
         "title":..., "url":..., "text": "<page content as pseudo-XML>"}.
         Child pages appear inside <content>...</content> as
         <page url="...">Title</page> tags -- verified live by creating a
         real child page and fetching the parent immediately afterward (the
         child appeared in fetch's "text" output before it appeared in
         search). This is what find_battlecard_page() parses to reliably
         find a competitor's battlecard child page under the configured
         parent, and what provisioning.py uses as its reachability check.
  - slackmcp_slack_search_users(query=)                            (SLACKMCP,
        connector "slackmcp")
      -> Verified live: a name query returned a markdown "results" string
         (not structured JSON) listing Name/User ID/Email/Permalink blocks,
         same markdown-search-result shape as slackmcp_slack_search_channels
         in the sibling repos.
  - slackmcp_slack_send_message(channel_id=, message=)             (SLACKMCP)
      -> Verified live: sent a real DM to a resolved Slack user ID and
         received back {"message_link":..., "message_context": {"message_ts":
         ..., "channel_id": "D..."}}. Passing a user ID as channel_id opens/
         posts to that user's DM channel.

  - Gong: the GONG provider's tool catalog IS discoverable in Scalekit even
    with ZERO connected accounts in this workspace (confirmed live via
    actions.tools.list_tools(filter=Filter(query="gong"), page_size=50),
    which returned all 34 real GONG/GONGMCP tool definitions, including
    input schemas, from the catalog -- this is a genuine tool-catalog lookup,
    not a guess). The relevant real tools and their verified input schemas:
      - gong_calls_list(from_date_time=, to_date_time=, workspace_id=,
            call_ids=, cursor=)
          REST GET /v2/calls (base_url https://api.gong.io), BASIC auth,
          assume_json_response=true. No MCP envelope (flat REST connector,
          same category as SALESFORCE/HUBSPOT in the sibling repo). No
          server-side keyword/tracker/mention filter parameter exists on
          this tool -- it only filters by date range, workspace, and
          explicit call_ids.
      - gong_calls_get(call_ids=[...] REQUIRED, from_date_time=, to_date_time=,
            workspace_id=, cursor=)
          REST POST /v2/calls/extensive. Verified jsonnet_template shows the
          real Gong API request shape: {"filter": {"callIds": [...],
          "fromDateTime":..., "toDateTime":..., "workspaceId":...}}. Gong's
          documented /v2/calls/extensive response (per Gong's own public API
          reference, which this REST passthrough wraps 1:1) includes a
          content.trackers array per call with tracker name + hit count,
          which is the closest real signal to "competitor mentioned in this
          call" available without transcript text-scanning.
      - gong_calls_transcript_get(call_ids=[...] REQUIRED, from_date_time=,
            to_date_time=, workspace_id=, cursor=)
          REST POST /v2/calls/transcript. Returns speaker-attributed,
          sentence-level transcript segments -- used as a client-side
          fallback to text-match configured competitor names when a call's
          tracker hits don't already identify which competitor was
          mentioned, or when no Gong tracker exists for a given competitor.
      - gong_trackers_list()
          REST GET /v2/settings/trackers. Lists tracker DEFINITIONS (name,
          tracked phrases) configured in the Gong account -- NOT which calls
          hit them; there is no separate "tracker hits by call" tool in this
          catalog. This agent uses it to build a tracker-name -> competitor
          mapping.
      - gong_users_list(cursor=, include_avatars=), gong_users_get(user_ids=,
            created_from_date_time=, created_to_date_time=, cursor=)
          REST GET/POST for resolving a Gong user ID on a call's parties
          list to an email address, used to identify the rep.

    The tool NAMES and SHAPES above are confirmed real (pulled straight from
    Scalekit's own tool catalog, not guessed) and have since been re-verified
    against real call data once a Gong connection was authorized in this
    workspace. GongConnector is built against these confirmed shapes;
    run_flow.py still treats any Gong call at runtime as capable of failing
    with a clear, specific, actionable error (see ConnectorUnavailableError
    below and Step 1 in run_flow.py), since a working connection today is not
    a guarantee it stays authorized on every future run. See README
    Prerequisites for exactly what connecting GONG in the Scalekit dashboard
    requires.
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
    ConnectorError so callers (namely run_flow.py's Step 1 Gong fetch) can
    tell "never configured" apart from "configured but currently failing" in
    their log messages and exit-code handling, without parsing error text.
    """


def _unwrap_mcp_envelope(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    MCP-based connectors (NOTIONMCP, SLACKMCP) return
    {"content": [{"type": "text", "text": "<json-or-plain-string>"}]} instead
    of a flat payload dict -- verified live against both. Plain REST
    connectors (GONG) return the flat payload directly, so this only unwraps
    when the envelope shape is actually present. If the decoded JSON is a
    bare list, it's wrapped as {"items": [...]} so callers always get a dict
    back.
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
            live-verified behavior for GONG in this workspace): logged as
            "not configured" rather than a generic error, since that's a
            distinct and expected state for a not-yet-set-up connector, not
            necessarily a broken one.
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


class GongConnector(Connector):
    """
    Gong API operations -- list calls in a lookback window, fetch enriched
    call details (tracker hits, participants), fetch transcripts, resolve
    Gong user IDs to emails.

    Tool names/shapes are verified against Scalekit's live tool catalog (see
    module docstring) and against real Gong call data once connected. Every
    method raises ConnectorUnavailableError (via execute_tool's
    RESOURCE_NOT_FOUND handling) rather than crashing with a raw SDK
    exception if called before GONG is connected -- callers (run_flow.py
    Step 1) are expected to catch that specifically and fail with a clear,
    actionable message rather than silently proceeding with fake data.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "GONG", workspace_id: str = ""):
        super().__init__(actions, connector_name, identifier)
        self.workspace_id = workspace_id or None

    def list_calls(self, from_date_time: str, to_date_time: str, cursor: Optional[str] = None) -> Dict:
        """
        List calls in [from_date_time, to_date_time] (ISO 8601). Returns the
        raw page dict (calls + a pagination cursor if more pages exist) --
        callers paginate via list_all_calls below rather than calling this
        directly in normal use.
        """
        kwargs: Dict[str, Any] = {"from_date_time": from_date_time, "to_date_time": to_date_time}
        if cursor:
            kwargs["cursor"] = cursor
        if self.workspace_id:
            kwargs["workspace_id"] = self.workspace_id
        return self.execute_tool("gong_calls_list", **kwargs) or {}

    def list_all_calls(self, from_date_time: str, to_date_time: str, max_pages: int = 20) -> List[Dict]:
        """
        Fetch every call in the lookback window, paginating via the cursor
        Gong's API returns until exhausted or max_pages is hit (a safety
        bound against an unexpectedly huge window rather than a normal
        limit -- 20 pages is generous for a weekly lookback).
        """
        calls: List[Dict] = []
        cursor = None
        for _ in range(max_pages):
            page = self.list_calls(from_date_time, to_date_time, cursor)
            batch = page.get("calls") or page.get("items") or []
            calls.extend(batch)
            cursor = page.get("cursor") or (page.get("records") or {}).get("cursor")
            if not cursor:
                break
        else:
            logger.warning(
                f"Reached max_pages={max_pages} while Gong still had more calls "
                f"(cursor present) -- results are truncated at {len(calls)} call(s). "
                f"Narrow LOOKBACK_DAYS or raise max_pages if this window is expected "
                f"to have this many calls."
            )
        return calls

    def get_calls_extensive(self, call_ids: List[str]) -> List[Dict]:
        """
        Fetch enriched details (participants, tracker hits, CRM associations)
        for up to a batch of call_ids via gong_calls_get. Returns the list of
        per-call detail records; malformed/missing entries are left to the
        caller to validate (see aggregator.py's per-call handling).
        """
        if not call_ids:
            return []
        kwargs: Dict[str, Any] = {"call_ids": call_ids}
        if self.workspace_id:
            kwargs["workspace_id"] = self.workspace_id
        data = self.execute_tool("gong_calls_get", **kwargs) or {}
        return data.get("calls") or data.get("items") or []

    def get_transcripts(self, call_ids: List[str]) -> List[Dict]:
        """
        Fetch transcripts for up to a batch of call_ids, used as a
        client-side fallback to text-match competitor names when a call's
        tracker hits (from get_calls_extensive) don't already resolve which
        competitor was mentioned.
        """
        if not call_ids:
            return []
        data = self.execute_tool("gong_calls_transcript_get", call_ids=call_ids) or {}
        return data.get("callTranscripts") or data.get("transcripts") or data.get("items") or []

    def list_trackers(self) -> List[Dict]:
        """List tracker (keyword tracker) definitions configured in the Gong account."""
        data = self.execute_tool("gong_trackers_list") or {}
        return data.get("trackers") or data.get("items") or []

    def get_users(self, user_ids: List[str]) -> List[Dict]:
        """
        Resolve Gong user IDs to full profiles including email/name. The
        primary real use is resolving metaData.primaryUserId (see
        build_user_lookup) since gong_calls_get never actually returns a
        populated parties list through this Scalekit tool wrapper.
        """
        if not user_ids:
            return []
        data = self.execute_tool("gong_users_get", user_ids=user_ids) or {}
        return data.get("users") or data.get("items") or []

    def build_user_lookup(self, user_ids: List[str]) -> Dict[str, Dict]:
        """
        Resolve a batch of Gong user IDs to {user_id: profile_dict}, used to
        turn a call's metaData.primaryUserId into a real name/email for
        identify_rep(). Malformed/missing individual user records are
        silently excluded rather than failing the whole batch -- callers
        treat a missing entry the same as "rep could not be identified".
        """
        users = self.get_users(user_ids)
        return {str(u.get("id")): u for u in users if isinstance(u, dict) and u.get("id")}


class NotionConnector(Connector):
    """
    Notion API operations (via the NotionMCP connector) -- find an existing
    competitor battlecard page under a configured parent page. This agent
    never creates or edits a battlecard: a PMM owns that content, this agent
    only looks one up and links to it.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "notionmcp-chAb8Lfz"):
        super().__init__(actions, connector_name, identifier)

    def verify_parent_page(self, parent_page_id: str) -> bool:
        """
        Best-effort reachability check for the configured battlecards parent
        page, used by provisioning.py at startup. notionmcp_notion-fetch
        returning without error (and a non-empty title) confirms both that
        the connected integration can read this page and that the ID is real.
        """
        try:
            data = self.execute_tool("notionmcp_notion-fetch", id=parent_page_id) or {}
        except ConnectorError:
            return False
        return bool(data.get("title"))

    def find_battlecard_page(self, parent_page_id: str, competitor_name: str) -> Optional[Dict]:
        """
        Look for a child page of parent_page_id whose title matches
        competitor_name (case-insensitive exact match preferred, otherwise a
        substring match, e.g. a battlecard titled "Salesforce Battlecard").
        Returns {"id":..., "title":..., "url":...} or None if no matching
        page is found -- this is a normal, expected outcome (not every
        competitor necessarily has a battlecard yet), never raised as an
        error.

        PRIMARY method: notion-fetch on parent_page_id, parsing the
        <page url="...">Title</page> child-page tags out of its rendered
        "text" field (see module docstring). This reflects newly created
        child pages immediately, unlike notion-search's eventually-consistent
        index.

        FALLBACK: if fetch finds no children at all (e.g. an empty or
        unreadable parent), a plain notion-search by competitor name is
        tried as a secondary signal, in case the battlecard exists somewhere
        outside the configured parent page (e.g. moved or miscategorized).
        """
        target = competitor_name.strip().casefold()

        try:
            fetch_data = self.execute_tool("notionmcp_notion-fetch", id=parent_page_id) or {}
        except ConnectorError as e:
            logger.warning(f"Could not fetch Notion parent page '{parent_page_id}': {e}")
            fetch_data = {}

        text = fetch_data.get("text", "") if isinstance(fetch_data, dict) else ""
        children = _parse_child_pages(text)

        for child_id, child_url, title in children:
            if title.strip().casefold() == target:
                return {"id": child_id, "title": title, "url": child_url}
        for child_id, child_url, title in children:
            if target in title.strip().casefold():
                return {"id": child_id, "title": title, "url": child_url}

        if children:
            # Parent page IS reachable and has children, just none matched --
            # a real "no battlecard for this competitor" outcome, not worth
            # a slower workspace-wide search fallback.
            return None

        logger.debug(
            f"notion-fetch found no child pages under parent '{parent_page_id}' -- "
            f"falling back to notion-search for '{competitor_name}'"
        )
        try:
            search_data = self.execute_tool("notionmcp_notion-search", query=competitor_name) or {}
        except ConnectorError as e:
            logger.warning(f"Notion search failed for competitor '{competitor_name}': {e}")
            return None

        results = search_data.get("results") or []
        if not results:
            return None

        exact = [r for r in results if (r.get("title") or "").strip().casefold() == target]
        match = exact[0] if exact else next(
            (r for r in results if target in (r.get("title") or "").strip().casefold()), None
        )
        if not match:
            return None
        return {"id": match.get("id"), "title": match.get("title"), "url": match.get("url")}


class SlackConnector(Connector):
    """
    Slack API operations (via the SlackMCP connector) -- resolve a rep's
    email/name to a Slack user ID and DM them a briefing digest.

    Only the SLACKMCP connector variant is ACTIVE in this workspace (the
    plain SLACK connector sits in PENDING_AUTH) -- verified live. Send-message
    params are channel_id/message, not channel/text; search-users returns
    markdown text, not structured JSON.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "slackmcp"):
        super().__init__(actions, connector_name, identifier)

    def resolve_user_id(self, email_or_name: str) -> Optional[str]:
        """
        Resolve a rep's email or display name to their Slack user ID. If
        email_or_name already looks like a raw Slack user ID (U...), it's
        used as-is without a search round-trip. Returns None if no match is
        found -- callers treat this as "skip this rep's DM, log a warning",
        never as a fatal error for the whole batch.
        """
        if not email_or_name:
            return None

        if email_or_name.startswith("U") and email_or_name.isalnum() and len(email_or_name) >= 9:
            return email_or_name

        data = self.execute_tool("slackmcp_slack_search_users", query=email_or_name) or {}
        results_text = data.get("results", "") if isinstance(data, dict) else ""
        return _extract_user_id_matching(results_text, email_or_name)

    def send_dm(self, user_id: str, text: str) -> Dict:
        """Send a direct message. Passing a user ID as channel_id opens/posts to their DM."""
        return self.execute_tool("slackmcp_slack_send_message", channel_id=user_id, message=text)


def _extract_user_id_matching(results_text: str, query: str) -> Optional[str]:
    """
    Parse a Slack user ID out of SlackMCP's markdown search-results text,
    e.g. "...Name: Jane Doe\\nUser ID: U0EXAMPLE1\\nEmail: jane@example.com...".

    When the query looks like an email, ONLY the result block whose Email
    line matches it exactly is used (search_users can return multiple
    partial-name matches, as verified live for a first-name query returning
    two different people) -- an email query with no exact match returns None
    rather than falling back to an arbitrary first result, since silently
    DMing the wrong person on an email lookup is worse than skipping the rep.
    The first-block fallback is reserved for name queries, where a fuzzy
    match has no better tiebreaker anyway.
    """
    blocks = re.split(r"\n---\n", results_text or "")
    query_lower = query.strip().lower()

    if "@" in query:
        for block in blocks:
            email_match = re.search(r"Email:\s*(\S+)", block)
            if email_match and email_match.group(1).strip().lower() == query_lower:
                user_id_match = re.search(r"User ID:\s*(\w+)", block)
                if user_id_match:
                    return user_id_match.group(1)
        return None

    first_match = re.search(r"User ID:\s*(\w+)", results_text or "")
    return first_match.group(1) if first_match else None


def _parse_child_pages(fetch_text: str) -> List[tuple]:
    """
    Parse <page url="...">Title</page> tags out of notion-fetch's rendered
    page text, returning [(page_id, url, title), ...]. Verified live: this
    is exactly how notion-fetch renders a page's child pages inside its
    <content>...</content> block.
    """
    results = []
    for match in re.finditer(r'<page url="([^"]+)">([^<]*)</page>', fetch_text or ""):
        url, title = match.group(1), match.group(2)
        page_id = _page_id_from_url(url)
        if page_id:
            results.append((page_id, url, title))
    return results


def _page_id_from_url(url: str) -> Optional[str]:
    """Extract a dash-formatted Notion page ID from a page URL's trailing 32-hex-char segment."""
    match = re.search(r"([0-9a-f]{32})(?:\?|$)", url)
    if not match:
        return None
    raw = match.group(1)
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"
