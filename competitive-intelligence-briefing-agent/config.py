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

        # Connector identities (the "identifier" each connected account is keyed by).
        # These can differ per connector if a shared service account authorizes
        # Gong/Notion/Slack instead of the PMM's own identity, but default to the
        # PMM's email for the common single-user case.
        self.gong_user = os.environ.get("GONG_USER")
        self.notion_user = os.environ.get("NOTION_USER")
        self.slack_user = os.environ.get("SLACK_USER")

        # Connector names -- must match the exact connection name shown in the
        # Scalekit dashboard, which is often auto-suffixed per workspace
        # (e.g. "notionmcp-chAb8Lfz"), not just the generic provider label.
        # Never hardcode a guessed connection name: verify yours via
        # list_connected_accounts / the dashboard before relying on defaults.
        #
        # Verified live against this workspace (env_20324953475777334) at
        # build time: "notionmcp-chAb8Lfz" and "slackmcp" are both ACTIVE.
        # GONG has NO connection at all in this workspace (get_or_create_
        # connected_account returns RESOURCE_NOT_FOUND for "gong"/"GONG"/
        # "gong-1"/"gongmcp", not merely PENDING_AUTH) -- the default below
        # is the plain provider label as a placeholder only. See README
        # Prerequisites for what a real deployment needs to set up first.
        self.gong_connector = os.environ.get("GONG_CONNECTOR", "GONG")
        self.notion_connector = os.environ.get("NOTION_CONNECTOR", "notionmcp-chAb8Lfz")
        self.slack_connector = os.environ.get("SLACK_CONNECTOR", "slackmcp")

        # The PMM this cycle runs on behalf of -- used as a label in logs and
        # as the state key for idempotency, not to scope Gong's call fetch
        # (Gong calls are fetched org-wide within the lookback window, then
        # grouped by whichever rep/competitor combination actually appears).
        self.pmm_email = os.environ.get("PMM_EMAIL")

        # How far back (in days) to search Gong for calls with competitor
        # mentions. 7 days is a reasonable default cadence for a weekly
        # competitive-intel digest; shorten for a daily polling cadence.
        self.lookback_days = self._parse_int("LOOKBACK_DAYS", 7, min_value=1)

        # Competitors to track, comma-separated (e.g. "Salesforce,HubSpot,Pipedrive").
        # The brief's example is "recent Salesforce mentions" but the agent
        # supports any number of competitors at once, since a PMM typically
        # owns several battlecards. Matching against Gong tracker hits and
        # transcript text is case-insensitive; see connectors.py.
        self.competitor_names = self._parse_list("COMPETITOR_NAMES") or ["Salesforce"]

        # Notion parent page under which one battlecard child page per
        # competitor already lives. This agent only ever searches for and
        # reads an existing battlecard under this page; it never creates one,
        # since a PMM should own battlecard authorship, not this agent (see
        # NotionConnector.find_battlecard_page).
        self.notion_battlecards_parent_page_id = os.environ.get("NOTION_BATTLECARDS_PARENT_PAGE_ID")

        # Gong workspace scoping (optional). Gong's tools accept an optional
        # workspace_id filter; leave blank to search across every workspace
        # the connected account can see.
        self.gong_workspace_id = os.environ.get("GONG_WORKSPACE_ID", "")

        # Timing / mode
        self.polling_mode = os.environ.get("POLLING_MODE", "false").lower() == "true"
        self.poll_interval_minutes = self._parse_int("POLL_INTERVAL_MINUTES", 60, min_value=1)
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

        if not self.gong_user:
            errors.append("GONG_USER")
        if not self.notion_user:
            errors.append("NOTION_USER")
        if not self.slack_user:
            errors.append("SLACK_USER")

        if not self.pmm_email:
            errors.append("PMM_EMAIL")
        if not self.notion_battlecards_parent_page_id:
            errors.append("NOTION_BATTLECARDS_PARENT_PAGE_ID")

        if errors:
            msg = f"Missing required config: {', '.join(errors)}"
            if logger:
                logger.error(msg)
            else:
                print(f"ERROR: {msg}")
            sys.exit(1)

        if not self.competitor_names:
            msg = "COMPETITOR_NAMES resolved to an empty list -- set at least one competitor name"
            if logger:
                logger.error(msg)
            else:
                print(f"ERROR: {msg}")
            sys.exit(1)

    def get_connector_users(self) -> Dict[str, str]:
        """Mapping of connector name -> identifier, for auth checks."""
        return {
            self.gong_connector: self.gong_user,
            self.notion_connector: self.notion_user,
            self.slack_connector: self.slack_user,
        }

    @staticmethod
    def _parse_list(key: str) -> Optional[List[str]]:
        raw = os.environ.get(key, "")
        if not raw.strip():
            return None
        return [item.strip() for item in raw.split(",") if item.strip()]

    @staticmethod
    def _parse_int(key: str, default: int, min_value: int = None) -> int:
        raw = os.environ.get(key, str(default))
        try:
            value = int(raw)
        except ValueError:
            msg = f"Invalid {key}: {raw!r} (must be an integer)"
            if logger:
                logger.error(msg)
            else:
                print(f"ERROR: {msg}")
            sys.exit(1)

        if min_value is not None and value < min_value:
            msg = f"{key} must be >= {min_value}, got {value}"
            if logger:
                logger.error(msg)
            else:
                print(f"ERROR: {msg}")
            sys.exit(1)

        return value
