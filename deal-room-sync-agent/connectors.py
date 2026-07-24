"""
Connector wrappers for Salesforce, Slack (via SlackMCP), and Google Drive.

All APIs go through Scalekit's actions.execute_tool().
No direct API imports, no token management, no credential storage in code.

Tool names and response shapes below are verified live against the Scalekit
environment env_20324953475777334 at build time -- not guessed:
  - salesforce_soql_execute(soql_query=...)            (SALESFORCE) -- flat dict,
    e.g. {"totalSize": N, "done": true, "records": [...]}. No MCP envelope.
  - salesforce_sobject_get / salesforce_sobject_update  (SALESFORCE) -- flat dict.
  - slackmcp_slack_search_public_and_private(query=...) (SLACKMCP)  -- MCP envelope
    {"content": [{"type": "text", "text": "<json-string>"}]}, and the parsed
    inner JSON is itself {"results": "<markdown-formatted text blob>",
    "pagination_info": "..."}. There is no structured per-message array --
    Slack search/read results come back as one formatted text block meant for
    display, not machine parsing, so excerpts are pulled out of that text.
  - slackmcp_slack_read_channel(channel_id=...)          (SLACKMCP)  -- same
    MCP envelope + text-blob shape, key is "messages" instead of "results".
  - slackmcp_slack_read_thread(channel_id=, message_ts=) (SLACKMCP)  -- same
    MCP envelope + text-blob shape.
  - slackmcp_slack_send_message(channel_id=, message=)   (SLACKMCP)  -- MCP envelope.
  - googledrive_search_files / get_file_metadata /       (GOOGLEDRIVE) -- flat
    create_file / create_folder / create_comment /        dict, plain REST
    list_comments / update_file_metadata / export_file     responses (no MCP
                                                             envelope). Confirmed
    live: GOOGLEDRIVE has no "mcp" suffix in its connector identifier and its
    responses are flat, matching the naming-pattern heuristic from the
    reference repo (AIRTABLE/GOOGLEFORMS are flat, NOTIONMCP/SLACKMCP are
    MCP-wrapped).
  - googledrive_export_file on a Google Doc returns essentially no usable body
    text (confirmed live: a fresh doc's export is just a byte-order-mark) --
    GOOGLEDRIVE cannot read or write a Google Doc's real body content. See
    GoogleDriveConnector.sync_deal_summary for how this agent works around it.
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
    MCP-based connectors (SLACKMCP, NOTIONMCP, ...) return
    {"content": [{"type": "text", "text": "<json-or-plain-string>"}]} instead
    of a flat payload dict -- verified live against SLACKMCP. Plain REST
    connectors (SALESFORCE, GOOGLEDRIVE) return the flat payload directly, so
    this only unwraps when the envelope shape is actually present.
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
                connection_name=self.connector_name,
                tool_input=kwargs,
            )
            return _unwrap_mcp_envelope(result.data or {})
        except Exception as e:
            logger.error(f"Tool execution failed: {tool_name}: {e}")
            raise ConnectorError(f"{tool_name} failed: {e}") from e


class SalesforceConnector(Connector):
    """
    Salesforce API operations -- opportunity context via SOQL.

    connector_name must match the exact connection name shown in the Scalekit
    dashboard (e.g. "salesforce-1"), not the generic "SALESFORCE" provider
    label -- Scalekit auto-suffixes connection names per workspace, and
    check_auth()'s get_or_create_connected_account call needs the exact name.
    Verified live: calling with the bare provider label 404s
    ("connection not found for the given key").
    """

    def __init__(self, actions, identifier: str, connector_name: str = "salesforce-1"):
        super().__init__(actions, connector_name, identifier)

    def run_soql(self, soql_query: str) -> List[Dict]:
        """Execute a SOQL query and return the records list (empty if none found)."""
        data = self.execute_tool("salesforce_soql_execute", soql_query=soql_query) or {}
        return data.get("records") or []

    def find_opportunity(
        self,
        opportunity_id: str = "",
        opportunity_name: str = "",
    ) -> Optional[Dict]:
        """
        Fetch one opportunity's context fields by Id (exact) or Name (LIKE match).

        Prefers an exact Id lookup when provided. Falls back to a case-insensitive
        partial Name match, returning the first hit (most recently modified) if
        several opportunities share similar names -- callers should prefer
        OPPORTUNITY_ID for unambiguous targeting in production.
        """
        fields = (
            "Id, Name, StageName, Amount, CloseDate, NextStep, "
            "Account.Name, Owner.Name, LastModifiedDate"
        )

        if opportunity_id:
            safe_id = opportunity_id.replace("'", "")
            records = self.run_soql(
                f"SELECT {fields} FROM Opportunity WHERE Id = '{safe_id}' LIMIT 1"
            )
            return records[0] if records else None

        if opportunity_name:
            safe_name = opportunity_name.replace("'", "\\'")
            records = self.run_soql(
                f"SELECT {fields} FROM Opportunity WHERE Name LIKE '%{safe_name}%' "
                f"ORDER BY LastModifiedDate DESC LIMIT 1"
            )
            return records[0] if records else None

        return None


class SlackConnector(Connector):
    """
    Slack API operations (via the SlackMCP connector) -- capturing key
    decisions from relevant discussion threads.

    Verified live: search and read tools return an MCP envelope wrapping a
    text blob (not structured per-message JSON), e.g.
    {"results": "# Search Results...\\n### Result 1...", "pagination_info": "..."}.
    Excerpts are extracted from that text rather than parsed as objects.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "slackmcp"):
        super().__init__(actions, connector_name, identifier)

    def search_relevant_messages(self, keyword: str, limit: int = 20) -> str:
        """Search public + private channels for messages mentioning `keyword`. Returns raw text blob."""
        data = self.execute_tool(
            "slackmcp_slack_search_public_and_private",
            query=keyword,
            limit=limit,
        ) or {}
        return data.get("results", "") or ""

    def read_channel(self, channel_id: str, limit: int = 20) -> str:
        """Read recent messages from a specific channel. Returns raw text blob."""
        data = self.execute_tool(
            "slackmcp_slack_read_channel",
            channel_id=channel_id,
            limit=limit,
        ) or {}
        return data.get("messages", "") or ""

    def read_thread(self, channel_id: str, message_ts: str) -> str:
        """Read a thread's parent + replies. Returns raw text blob."""
        data = self.execute_tool(
            "slackmcp_slack_read_thread",
            channel_id=channel_id,
            message_ts=message_ts,
        ) or {}
        return data.get("messages", "") or ""

    def send_message(self, channel_id: str, message: str) -> Dict:
        """Post a message to a channel or DM (pass a user ID as channel_id for a DM)."""
        return self.execute_tool(
            "slackmcp_slack_send_message",
            channel_id=channel_id,
            message=message,
        )


class GoogleDriveConnector(Connector):
    """
    Google Drive API operations -- the deal room doc.

    connector_name must match the exact connection name shown in the Scalekit
    dashboard (e.g. "googledrive-9WdQ8yGN"), same caveat as SalesforceConnector.

    Known API gap (verified live, not assumed): GOOGLEDRIVE has no tool that
    reads or writes a Google Doc's actual body text. googledrive_export_file
    on a real Google Doc returns no usable content (just a byte-order-mark on
    a fresh doc), and googledrive_create_file only creates file *metadata* --
    it explicitly cannot upload document body content (that requires a
    multipart media upload the tool doesn't support). This agent therefore
    syncs the deal summary as a Google Drive **comment** on the deal room file
    via googledrive_create_comment, confirmed live to work cleanly and show
    up immediately via googledrive_list_comments. This keeps a running,
    timestamped log of every sync in the doc's comment sidebar without ever
    overwriting the doc body -- the only sync approach that works today with
    zero extra setup beyond authorizing GOOGLEDRIVE itself. A GOOGLEDOCS
    connector exists in Scalekit's catalog for true body-content edits, but it
    is a separate connector this agent does not require.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "googledrive"):
        super().__init__(actions, connector_name, identifier)

    def find_file_by_name(self, name: str, folder_id: str = "") -> Optional[Dict]:
        """Search for a non-trashed file with this exact name, optionally scoped to a folder."""
        safe_name = name.replace("'", "\\'")
        query = f"name = '{safe_name}' and trashed = false"
        if folder_id:
            query += f" and '{folder_id}' in parents"

        data = self.execute_tool(
            "googledrive_search_files",
            query=query,
            page_size=5,
            fields="files(id,name,mimeType)",
        ) or {}
        files = data.get("files") or []
        return files[0] if files else None

    def create_deal_room_doc(self, name: str, folder_id: str = "", description: str = "") -> Dict:
        """
        Create a new Google Doc to serve as the deal room file (metadata only --
        the doc body will be empty; see class docstring for why body content
        cannot be populated via GOOGLEDRIVE, and how this agent works around it).
        """
        kwargs: Dict[str, Any] = {
            "name": name,
            "mime_type": "application/vnd.google-apps.document",
        }
        if folder_id:
            kwargs["parent_folder_id"] = folder_id
        if description:
            kwargs["description"] = description
        return self.execute_tool("googledrive_create_file", **kwargs)

    def find_or_create_deal_room_doc(
        self, name: str, folder_id: str = "", description: str = ""
    ) -> Dict:
        """Find the deal room doc by name, creating it if it doesn't exist yet."""
        existing = self.find_file_by_name(name, folder_id)
        if existing:
            logger.info(f"Using existing deal room doc '{name}' ({existing.get('id')})")
            return existing

        logger.warning(f"Deal room doc '{name}' not found -- creating it now")
        return self.create_deal_room_doc(name, folder_id, description)

    def get_file_metadata(self, file_id: str) -> Dict:
        """Fetch a file's metadata, used to confirm a configured DEAL_ROOM_DOC_ID is valid."""
        return self.execute_tool("googledrive_get_file_metadata", file_id=file_id)

    def sync_deal_summary(self, file_id: str, summary_text: str) -> Dict:
        """
        Post the deal summary as a new comment on the deal room doc. Chosen
        over metadata-description sync because comments are more visible (they
        show in the doc's comment sidebar) and preserve a running history
        rather than overwriting a single field on every sync.
        """
        return self.execute_tool("googledrive_create_comment", file_id=file_id, content=summary_text)


# Matches Slack's "### Result N of M" / "=== Message from ..." block headers,
# used to split a raw search/read text blob into individual message excerpts.
_SLACK_MESSAGE_BLOCK_PATTERN = re.compile(
    r"(?:^###\s*Result\s+\d+.*$|^===\s*Message from.*===\s*$)",
    re.MULTILINE,
)

# Slack's own "no results" text, verified live (e.g. searching a keyword with
# zero matches returns "# Search Results for: X\n\nNo results found.\n").
# Without this check, that sentence would otherwise be treated as one "real"
# excerpt instead of correctly being recognized as zero context found.
_NO_RESULTS_PATTERN = re.compile(r"no results found", re.IGNORECASE)


def split_slack_text_blob(raw_text: str) -> List[str]:
    """
    Split a Slack search/read text blob (Markdown-ish, meant for display) into
    individual message excerpts, trimming the block headers Slack's MCP tools
    prepend to each result. Best-effort: if no recognizable headers are found,
    returns the whole blob as a single excerpt.
    """
    if not raw_text or not raw_text.strip():
        return []

    if _NO_RESULTS_PATTERN.search(raw_text) and not _SLACK_MESSAGE_BLOCK_PATTERN.search(raw_text):
        return []

    has_headers = bool(_SLACK_MESSAGE_BLOCK_PATTERN.search(raw_text))
    blocks = _SLACK_MESSAGE_BLOCK_PATTERN.split(raw_text)
    excerpts = [b.strip() for b in blocks if b and b.strip()]

    if not excerpts:
        return [raw_text.strip()]
    if has_headers:
        # The text before the first header is a "# Search Results for: ..."
        # title/preamble, not a real message -- drop it once real message
        # blocks were actually found via the header pattern.
        excerpts = excerpts[1:] if len(excerpts) > 1 else excerpts
    return excerpts if excerpts else [raw_text.strip()]
