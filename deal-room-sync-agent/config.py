"""
Configuration management with validation.

All settings loaded from environment variables.
Validates on startup and provides clear error messages.
"""

import os
import sys
from typing import Dict, Optional

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
        # Usually all the same AE, but kept separate in case a shared service
        # account is used for one connector.
        self.salesforce_user = os.environ.get("SALESFORCE_USER")
        self.slack_user = os.environ.get("SLACK_USER")
        self.google_drive_user = os.environ.get("GOOGLE_DRIVE_USER")

        # Connector names -- must match the exact connection name shown in the
        # Scalekit dashboard, which is often auto-suffixed per workspace
        # (e.g. "salesforce-1", "googledrive-9WdQ8yGN"), not just the generic
        # provider label. Verified live: "SALESFORCE" and "GOOGLEDRIVE" alone
        # 404 against get_or_create_connected_account in this workspace.
        self.salesforce_connector = os.environ.get("SALESFORCE_CONNECTOR", "salesforce-1")
        self.slack_connector = os.environ.get("SLACK_CONNECTOR", "slackmcp")
        self.google_drive_connector = os.environ.get("GOOGLE_DRIVE_CONNECTOR", "googledrive")

        # The AE this run is on behalf of. Used for logging/attribution only;
        # the identifiers above drive which connected accounts are used.
        self.ae_email = os.environ.get("AE_EMAIL")

        # Which opportunity/deal room this run targets. Either an explicit
        # Salesforce Opportunity Id, or a Name to look up via SOQL LIKE match.
        # At least one must be set.
        self.opportunity_id = os.environ.get("OPPORTUNITY_ID", "")
        self.opportunity_name = os.environ.get("OPPORTUNITY_NAME", "")

        # Google Drive destination for the deal room doc/comment log. Either an
        # existing file's ID, or a name to find-or-create under DEAL_ROOM_FOLDER_ID.
        self.deal_room_doc_id = os.environ.get("DEAL_ROOM_DOC_ID", "")
        self.deal_room_folder_id = os.environ.get("DEAL_ROOM_FOLDER_ID", "")
        self.deal_room_doc_name = os.environ.get("DEAL_ROOM_DOC_NAME", "")

        # Slack discovery: which channel(s) to search for relevant discussion.
        # If SLACK_CHANNEL_ID is set, search is scoped to that channel via
        # slack_read_channel; otherwise the agent falls back to a workspace-wide
        # search (slackmcp_slack_search_public_and_private) keyed on the deal's
        # search keyword (defaults to the opportunity/account name).
        self.slack_channel_id = os.environ.get("SLACK_CHANNEL_ID", "")
        self.slack_search_keyword = os.environ.get("SLACK_SEARCH_KEYWORD", "")
        self.slack_message_limit = self._parse_int("SLACK_MESSAGE_LIMIT", 20, min_value=1)

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

        if not self.salesforce_user:
            errors.append("SALESFORCE_USER")
        if not self.slack_user:
            errors.append("SLACK_USER")
        if not self.google_drive_user:
            errors.append("GOOGLE_DRIVE_USER")

        if not self.ae_email:
            errors.append("AE_EMAIL")

        if not self.opportunity_id and not self.opportunity_name:
            errors.append("OPPORTUNITY_ID or OPPORTUNITY_NAME (at least one required)")

        if not self.deal_room_doc_id and not self.deal_room_doc_name:
            errors.append("DEAL_ROOM_DOC_ID or DEAL_ROOM_DOC_NAME (at least one required)")

        if errors:
            msg = f"Missing required config: {', '.join(errors)}"
            if logger:
                logger.error(msg)
            else:
                print(f"ERROR: {msg}")
            sys.exit(1)

    def get_connector_users(self) -> Dict[str, str]:
        """Mapping of connector name -> identifier, for auth checks."""
        return {
            self.salesforce_connector: self.salesforce_user,
            self.slack_connector: self.slack_user,
            self.google_drive_connector: self.google_drive_user,
        }

    def effective_search_keyword(self, opportunity_name: str = "") -> str:
        """The keyword used to find relevant Slack discussion for this deal."""
        if self.slack_search_keyword:
            return self.slack_search_keyword
        if opportunity_name:
            return opportunity_name
        return self.opportunity_name

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
