"""
Connector wrappers for GitHub, Linear, Confluence, and Notion.

All APIs go through Scalekit's actions.execute_tool(). No direct API imports,
no token management, no credential storage in code.

Tool names and parameter shapes below are verified live against this
workspace's Scalekit environment (env_20324953475777334) at build time -- not
guessed. The four shapes most likely to bite are called out explicitly:

  - github_tags_list(owner=..., repo=..., per_page=...)          (GITHUB connector)
  - github_release_get_latest(owner=..., repo=...)               (GITHUB)
      NOTE: 404s on repos that tag without cutting GitHub Releases, so tag
      resolution falls back to github_tags_list -- verified live against a
      repo with tags but no releases.
  - github_commits_compare(owner=..., repo=..., basehead="v1...v2")  (GITHUB)
      NOTE: there is NO base/head pair. It is ONE string in "BASE...HEAD"
      form (three dots). Verified live: "v1.2.0...v1.2.1" -> 3 commits.
  - github_pull_requests_list(owner=..., repo=..., state="closed",
        sort="updated", direction="desc", per_page=...)          (GITHUB)
      NOTE: the API has no "merged" filter -- state="closed" includes closed
      -but-never-merged PRs, so callers MUST filter on merged_at themselves.

  - linear_issue_get(issueId="ENG-123")                          (LINEAR connector)
      NOTE: GraphQL under the hood; Linear's issue(id:) resolver accepts a
      human identifier as well as a UUID, so "ENG-123" works directly. The
      baked-in selection set does NOT include the `identifier` field, so the
      caller keeps the identifier it searched with.

  - confluence_space_list(keys=[...], limit=...)                 (CONFLUENCE connector)
      -> {"results": [{"id": "294916", "key": "SD", ...}]}
      NOTE: confluence_page_create needs the NUMERIC space id, not the key.
  - confluence_page_create(spaceId=..., title=..., parentId=...,
        body_representation="storage", body_value="<p>...</p>")  (CONFLUENCE)
      NOTE: spaceId/parentId are camelCase while body_* are snake_case, and
      body_representation + body_value must BOTH be present or no body is
      sent at all.

  - notion_page_create(parent_page_id=..., properties={...},
        child_blocks=[<raw notion blocks>])                      (NOTION connector)
  - notion_page_content_append(block_id=..., blocks=[{"type","text"}])  (NOTION)
      NOTE: these two take DIFFERENT block formats. child_blocks is the raw
      Notion block object shape; blocks is a simplified {type, text} shape
      that the connector converts server-side. Mixing them up silently fails.
"""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ConnectorError(Exception):
    """Raised when a connector operation fails and there is no safe fallback."""


def _unwrap_mcp_envelope(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    MCP-based connectors return
    {"content": [{"type": "text", "text": "<json-or-plain-string>"}]} instead
    of a flat payload dict. Plain REST connectors (GITHUB, LINEAR, CONFLUENCE,
    NOTION) return the flat payload directly, so this only unwraps when the
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
        return json.loads(text)
    except (TypeError, ValueError):
        return {"text": text}


def _as_list(data: Any, *keys: str) -> List[Dict]:
    """
    Normalize a payload that may be a bare JSON array, a Scalekit-wrapped
    array ({"array": [...]}), or an object holding the list under one of
    `keys`. GitHub's list endpoints return bare arrays that Scalekit wraps
    under "array" -- verified live -- while Confluence and Notion use
    "results", so both shapes are handled rather than assuming one.
    """
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("array",) + keys:
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


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
            # A missing connection (RESOURCE_NOT_FOUND) is a different problem
            # from an unauthorized one, and the fix is different too.
            detail = str(e)
            if "NOT_FOUND" in detail:
                logger.error(
                    f"Connection '{self.connector_name}' does not exist in this Scalekit "
                    f"workspace. Create it under Agent Auth > Connections, or point the "
                    f"matching *_CONNECTOR env var at the name your workspace actually uses."
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
        """Execute a Scalekit tool and return the data payload.

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
            logger.debug(f"Tool execution failed: {tool_name}: {e}")
            raise ConnectorError(f"{tool_name} failed: {e}") from e


class GitHubConnector(Connector):
    """GitHub API operations -- resolve release tags and collect merged PRs."""

    def __init__(self, actions, identifier: str, connector_name: str = "github"):
        super().__init__(actions, connector_name, identifier)

    def latest_release_tag(self, owner: str, repo: str) -> Optional[str]:
        """
        Return the tag of the most recent published GitHub Release, or None.

        Many repos tag without ever cutting a Release object, in which case
        this endpoint 404s. That is an expected outcome, not an error, so the
        caller falls back to list_tags().
        """
        try:
            data = self.execute_tool("github_release_get_latest", owner=owner, repo=repo)
        except ConnectorError:
            return None
        tag = (data or {}).get("tag_name")
        return tag or None

    def list_tags(self, owner: str, repo: str, per_page: int = 30) -> List[Dict]:
        """List tags, newest first (GitHub returns them in that order)."""
        data = self.execute_tool(
            "github_tags_list", owner=owner, repo=repo, per_page=per_page
        )
        return _as_list(data, "tags")

    def compare(self, owner: str, repo: str, base: str, head: str, per_page: int = 100) -> Dict:
        """
        Compare two refs and return the raw comparison payload.

        `basehead` is a single "BASE...HEAD" string -- there is no base/head
        parameter pair on this tool. Verified live.
        """
        return self.execute_tool(
            "github_commits_compare",
            owner=owner,
            repo=repo,
            basehead=f"{base}...{head}",
            per_page=per_page,
        )

    def list_merged_pull_requests(
        self, owner: str, repo: str, per_page: int = 100, base: Optional[str] = None
    ) -> List[Dict]:
        """
        List recently-updated closed PRs that were actually merged.

        GitHub's pulls endpoint has no "merged" filter, so `state="closed"`
        also returns PRs that were closed without merging. Those are dropped
        here on `merged_at` rather than being written into a changelog as if
        they had shipped.
        """
        kwargs = dict(
            owner=owner, repo=repo, state="closed",
            sort="updated", direction="desc", per_page=per_page,
        )
        if base:
            kwargs["base"] = base
        data = self.execute_tool("github_pull_requests_list", **kwargs)
        return [pr for pr in _as_list(data, "pulls") if pr.get("merged_at")]

    def pull_requests_for_commit(self, owner: str, repo: str, sha: str) -> List[Dict]:
        """
        Return the PR(s) a commit belongs to.

        This is the precise path from "commit in the release range" back to
        "the PR that introduced it", used when a commit's message has no
        squash-merge "(#123)" suffix to parse.
        """
        data = self.execute_tool(
            "github_commit_pull_requests_list", owner=owner, repo=repo, commit_sha=sha
        )
        return _as_list(data, "pulls")


class LinearConnector(Connector):
    """
    Linear API operations -- resolve an issue from its human identifier.

    Every Linear tool is GraphQL under the hood (POST api.linear.app/graphql)
    with a hardcoded query per tool; the flat params map into GraphQL
    variables server-side.
    """

    def __init__(self, actions, identifier: str, connector_name: str = "linear"):
        super().__init__(actions, connector_name, identifier)

    def get_issue(self, issue_identifier: str) -> Optional[Dict]:
        """
        Fetch one issue by identifier ("ENG-123") or UUID.

        Linear's issue(id:) resolver accepts the human identifier directly, so
        no separate lookup is needed. Returns None when the issue does not
        exist or is not visible to this account -- a stale reference in an old
        PR title must not abort the whole changelog.
        """
        try:
            data = self.execute_tool("linear_issue_get", issueId=issue_identifier)
        except ConnectorError as e:
            logger.debug(f"Linear lookup failed for {issue_identifier}: {e}")
            return None

        issue = (data or {}).get("issue")
        if not issue:
            # Some responses nest under data.issue depending on the passthrough.
            issue = ((data or {}).get("data") or {}).get("issue")
        return issue if isinstance(issue, dict) else None


class ConfluenceConnector(Connector):
    """Confluence API operations -- resolve a space and publish the changelog page."""

    def __init__(self, actions, identifier: str, connector_name: str = "confluence"):
        super().__init__(actions, connector_name, identifier)

    def list_spaces(self, limit: int = 250) -> List[Dict]:
        """List spaces visible to the connected account."""
        data = self.execute_tool("confluence_space_list", limit=limit)
        return _as_list(data, "results")

    def resolve_space_id(self, space_key: str) -> Optional[str]:
        """
        Map a human space key ("SD") to the numeric space id the page-create
        API requires. Returns None when the key is not visible.
        """
        for space in self.list_spaces():
            if str(space.get("key", "")).lower() == space_key.lower():
                return str(space.get("id")) if space.get("id") is not None else None
        return None

    def create_page(
        self,
        space_id: str,
        title: str,
        body_html: str,
        parent_id: Optional[str] = None,
    ) -> Dict:
        """
        Create a published Confluence page holding the changelog.

        `body_representation` and `body_value` must both be supplied or the
        connector omits the body entirely, producing a blank page. "storage"
        is Confluence's XHTML-ish format, which is far easier to generate
        correctly than serialized ADF JSON.
        """
        payload: Dict[str, Any] = {
            "spaceId": space_id,          # camelCase
            "title": title,
            "status": "current",
            "body_representation": "storage",  # snake_case -- yes, really
            "body_value": body_html,
        }
        if parent_id:
            payload["parentId"] = parent_id
        return self.execute_tool("confluence_page_create", **payload)

    def find_page_by_title(self, space_key: str, title: str) -> Optional[Dict]:
        """
        Look for an existing page with this exact title in the space.

        Used to avoid publishing a second copy of the same release's
        changelog. CQL string values are quoted, so embedded quotes are
        escaped rather than allowed to terminate the literal early.
        """
        cql = f'type=page AND space="{_escape_cql(space_key)}" AND title="{_escape_cql(title)}"'
        try:
            data = self.execute_tool("confluence_search", cql=cql, limit=5)
        except ConnectorError as e:
            logger.debug(f"Confluence title search failed: {e}")
            return None

        for result in _as_list(data, "results"):
            # v1 search nests the page under "content"; tolerate both shapes.
            content = result.get("content") if isinstance(result.get("content"), dict) else result
            if str(content.get("title", "")).strip() == title.strip():
                return content
        return None


class NotionConnector(Connector):
    """Notion API operations -- publish the changelog as a child page."""

    def __init__(self, actions, identifier: str, connector_name: str = "notion"):
        super().__init__(actions, connector_name, identifier)

    def create_page(self, parent_page_id: str, title: str, blocks: List[Dict]) -> Dict:
        """
        Create a child page under `parent_page_id` with `blocks` as content.

        `child_blocks` here takes RAW Notion block objects (see
        notion_blocks.py), which is a different shape from the simplified
        {type, text} format that notion_page_content_append expects. Notion
        caps children at 100 blocks per request, so any overflow is appended
        in follow-up calls by the caller.
        """
        return self.execute_tool(
            "notion_page_create",
            parent_page_id=parent_page_id,
            properties={
                # The title key must literally be "title", not the display name.
                "title": {"title": [{"text": {"content": title[:2000]}}]}
            },
            child_blocks=blocks,
            icon={"type": "emoji", "emoji": "🚀"},
        )

    def append_blocks(self, block_id: str, blocks: List[Dict]) -> Dict:
        """
        Append raw Notion blocks to an existing page via the data passthrough.

        notion_page_content_append exists but takes a simplified {type, text}
        shape that cannot express links, which a changelog needs for every PR
        reference. Going through notion_data_fetch keeps one block format
        (raw) across create and append instead of maintaining two renderers.
        """
        return self.execute_tool(
            "notion_data_fetch",
            method="PATCH",
            path=f"/blocks/{block_id}/children",
            body={"children": blocks},
        )

    def search_pages(self, query: str, page_size: int = 10) -> List[Dict]:
        """Search pages by title text. Returns pages only, never databases."""
        data = self.execute_tool("notion_page_search", query=query, page_size=page_size)
        return _as_list(data, "results")


def _escape_cql(value: str) -> str:
    """
    Escape a value for safe interpolation into a double-quoted CQL string.

    Backslashes first, then quotes, so a title containing a quote cannot
    close the literal early and inject extra CQL clauses. Release titles are
    derived from config and PR data, so they are treated as untrusted.
    """
    return str(value).replace("\\", "\\\\").replace('"', '\\"')
