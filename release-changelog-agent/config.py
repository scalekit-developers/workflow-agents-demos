"""
Configuration management with validation.

All settings loaded from environment variables.
Validates on startup and provides clear error messages.
"""

import os
import sys
from typing import Dict, List, Optional

logger = None  # Set by run_flow after logging is initialized


class Config:
    """Application configuration."""

    def __init__(self):
        """Load configuration from environment variables."""
        # Scalekit
        self.scalekit_env_url = os.environ.get("SCALEKIT_ENV_URL")
        self.scalekit_client_id = os.environ.get("SCALEKIT_CLIENT_ID")
        self.scalekit_client_secret = os.environ.get("SCALEKIT_CLIENT_SECRET")

        # Connector identities (the "identifier" each connected account is keyed by)
        self.github_user = os.environ.get("GITHUB_USER")
        self.linear_user = os.environ.get("LINEAR_USER")
        self.confluence_user = os.environ.get("CONFLUENCE_USER")
        self.notion_user = os.environ.get("NOTION_USER")

        # Connector names -- must match the EXACT connection name shown in the
        # Scalekit dashboard under Agent Auth > Connections. Scalekit
        # auto-suffixes these per workspace (e.g. "github-a1b2c3d4"), so there
        # is deliberately no default: a hardcoded guess would point at a
        # connection that does not exist in the reader's workspace and fail
        # with a confusing RESOURCE_NOT_FOUND rather than a clear config error.
        self.github_connector = os.environ.get("GITHUB_CONNECTOR", "")
        self.linear_connector = os.environ.get("LINEAR_CONNECTOR", "")
        self.confluence_connector = os.environ.get("CONFLUENCE_CONNECTOR", "")
        self.notion_connector = os.environ.get("NOTION_CONNECTOR", "")

        # Release manager this changelog is attributed to (shown in the footer
        # of the published page and used as the state key).
        self.release_manager = os.environ.get("RELEASE_MANAGER")

        # Source repository.
        self.github_owner = os.environ.get("GITHUB_OWNER")
        self.github_repo = os.environ.get("GITHUB_REPO")

        # The release being written up. RELEASE_VERSION is the label ("v2.4.0")
        # that titles the changelog. PREVIOUS_TAG / CURRENT_TAG bound the commit
        # range; when left blank the agent resolves them from the repo's tags
        # (see provisioning.resolve_release_range).
        self.release_version = os.environ.get("RELEASE_VERSION", "")
        self.previous_tag = os.environ.get("PREVIOUS_TAG", "")
        self.current_tag = os.environ.get("CURRENT_TAG", "")

        # Confluence destination. CONFLUENCE_SPACE_KEY is the human key ("SD");
        # it is resolved to the numeric spaceId that the create-page API needs.
        self.confluence_space_key = os.environ.get("CONFLUENCE_SPACE_KEY", "")
        self.confluence_parent_id = os.environ.get("CONFLUENCE_PARENT_ID", "")

        # Notion destination -- the page the changelog is created UNDER.
        # A database id is not used: changelogs are documents, not rows, and
        # notion_page_create needs one or the other, never both.
        self.notion_parent_page_id = os.environ.get("NOTION_PARENT_PAGE_ID", "")

        # Publish targets. Either can be disabled independently, so a team that
        # only uses one tool does not need a dummy destination for the other.
        self.publish_confluence = (
            os.environ.get("PUBLISH_CONFLUENCE", "true").lower() == "true"
        )
        self.publish_notion = os.environ.get("PUBLISH_NOTION", "true").lower() == "true"

        # Linear enrichment. On by default. When disabled the changelog still
        # links every PR, it just omits the Linear issue references -- useful
        # if your workspace has no LINEAR connection configured.
        self.enable_linear = os.environ.get("ENABLE_LINEAR", "true").lower() == "true"

        # Regex-ready list of Linear team prefixes (e.g. "ENG,PLAT") used to
        # spot issue identifiers like ENG-123 in PR titles and branch names.
        # Empty means "any uppercase prefix", which is the common case.
        self.linear_team_prefixes = self._parse_list("LINEAR_TEAM_PREFIXES") or []

        # Max PRs pulled per run (GitHub caps per_page at 100).
        self.max_prs = self._parse_int("MAX_PRS", 100, min_value=1, max_value=100)

        # Timing / mode
        self.dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
        self.log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    def validate(self):
        """Validate required configuration. Fails fast if missing."""
        errors = []

        if not self.scalekit_env_url:
            errors.append("SCALEKIT_ENV_URL")
        if not self.scalekit_client_id:
            errors.append("SCALEKIT_CLIENT_ID")
        if not self.scalekit_client_secret:
            errors.append("SCALEKIT_CLIENT_SECRET")

        if not self.github_user:
            errors.append("GITHUB_USER")
        if not self.github_connector:
            errors.append("GITHUB_CONNECTOR")
        if not self.release_manager:
            errors.append("RELEASE_MANAGER")
        if not self.github_owner:
            errors.append("GITHUB_OWNER")
        if not self.github_repo:
            errors.append("GITHUB_REPO")

        # Only demand an identity for connectors that are actually going to be used.
        if self.enable_linear and not self.linear_user:
            errors.append("LINEAR_USER (or set ENABLE_LINEAR=false)")
        if self.enable_linear and not self.linear_connector:
            errors.append("LINEAR_CONNECTOR (or set ENABLE_LINEAR=false)")
        if self.publish_confluence and not self.confluence_user:
            errors.append("CONFLUENCE_USER (or set PUBLISH_CONFLUENCE=false)")
        if self.publish_confluence and not self.confluence_connector:
            errors.append("CONFLUENCE_CONNECTOR (or set PUBLISH_CONFLUENCE=false)")
        if self.publish_notion and not self.notion_user:
            errors.append("NOTION_USER (or set PUBLISH_NOTION=false)")
        if self.publish_notion and not self.notion_connector:
            errors.append("NOTION_CONNECTOR (or set PUBLISH_NOTION=false)")

        if self.publish_confluence and not self.confluence_space_key:
            errors.append("CONFLUENCE_SPACE_KEY (or set PUBLISH_CONFLUENCE=false)")
        if self.publish_notion and not self.notion_parent_page_id:
            errors.append("NOTION_PARENT_PAGE_ID (or set PUBLISH_NOTION=false)")

        if errors:
            msg = f"Missing required config: {', '.join(errors)}"
            self._fail(msg)

        # Publishing nowhere still does useful work in --dry-run (it prints the
        # rendered changelog), but as a real run it would silently do nothing.
        if not self.publish_confluence and not self.publish_notion and not self.dry_run:
            self._fail(
                "Both PUBLISH_CONFLUENCE and PUBLISH_NOTION are false -- the agent would "
                "generate a changelog and publish it nowhere. Enable at least one, or use "
                "--dry-run to preview."
            )

    @staticmethod
    def _fail(msg: str):
        if logger:
            logger.error(msg)
        else:
            print(f"ERROR: {msg}")
        sys.exit(1)

    def get_connector_users(self) -> Dict[str, str]:
        """Mapping of connector name -> identifier, for the auth check.

        Only includes connectors this run will actually touch, so a disabled
        integration never reports a scary auth warning.
        """
        mapping = {self.github_connector: self.github_user}
        if self.enable_linear:
            mapping[self.linear_connector] = self.linear_user
        if self.publish_confluence:
            mapping[self.confluence_connector] = self.confluence_user
        if self.publish_notion:
            mapping[self.notion_connector] = self.notion_user
        return mapping

    @staticmethod
    def _parse_list(key: str) -> Optional[List[str]]:
        raw = os.environ.get(key, "")
        if not raw.strip():
            return None
        return [item.strip() for item in raw.split(",") if item.strip()]

    @staticmethod
    def _parse_int(key: str, default: int, min_value: int = None, max_value: int = None) -> int:
        raw = os.environ.get(key, str(default))
        try:
            value = int(raw)
        except ValueError:
            Config._fail(f"Invalid {key}: {raw!r} (must be an integer)")

        if min_value is not None and value < min_value:
            Config._fail(f"{key} must be >= {min_value}, got {value}")
        if max_value is not None and value > max_value:
            Config._fail(f"{key} must be <= {max_value}, got {value}")

        return value
