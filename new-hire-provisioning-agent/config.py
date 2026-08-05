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

        # Connector identities (the "identifier" each connected account is keyed by)
        self.gusto_user = os.environ.get("GUSTO_USER")
        self.notion_user = os.environ.get("NOTION_USER")
        self.slack_user = os.environ.get("SLACK_USER")
        self.google_workspace_user = os.environ.get("GOOGLE_WORKSPACE_USER")

        # Connector names -- must match the exact connection name shown in the
        # Scalekit dashboard, which is often auto-suffixed per workspace
        # (e.g. "gustomcp-SoSOMZ20", "notionmcp-chAb8Lfz"), not just the generic
        # provider label. Verified live against this workspace's connections.
        self.gusto_connector = os.environ.get("GUSTO_CONNECTOR", "gustomcp-SoSOMZ20")
        self.notion_connector = os.environ.get("NOTION_CONNECTOR", "notionmcp-chAb8Lfz")
        self.slack_connector = os.environ.get("SLACK_CONNECTOR", "slackmcp")

        # Google Workspace via Domain-Wide Delegation. The connector identifier
        # in Scalekit's catalog is "GOOGLEDWD" ("Google Workspace (DWD)"), but
        # it shows setup: not_configured in most workspaces until an admin
        # completes the GCP service account + DWD setup (see README
        # Prerequisites) and connects it in the Scalekit dashboard. Until then,
        # get_or_create_connected_account for this connector returns
        # RESOURCE_NOT_FOUND, which this agent treats as "not authorized yet"
        # rather than a crash -- see connectors.py GoogleWorkspaceConnector and
        # run_flow.py Step 2.
        self.google_workspace_connector = os.environ.get("GOOGLE_WORKSPACE_CONNECTOR", "googledwd")

        # HR admin whose identity this agent runs on behalf of. Used only as a
        # label in logs/state, since Gusto queries are org-wide (all employees
        # in the connected company), not scoped to a single admin's records.
        self.hr_admin_email = os.environ.get("HR_ADMIN_EMAIL")

        # Optional one-shot overrides to target a SPECIFIC new hire instead of
        # scanning Gusto for all not-yet-provisioned employees. If neither is
        # set, the agent scans (see run_flow.py Step 1 / connectors.py
        # GustoConnector.find_new_hires).
        self.new_hire_employee_id = os.environ.get("NEW_HIRE_EMPLOYEE_ID", "")
        self.new_hire_name = os.environ.get("NEW_HIRE_NAME", "")

        # Notion destination -- an existing page under which onboarding docs
        # get created as child pages. NOTION_TEMPLATE_PAGE_ID is accepted as an
        # alias for the same setting (some HR admins think of it as "the
        # onboarding doc template/hub page"); NOTION_PARENT_PAGE_ID takes
        # precedence if both are set.
        self.notion_parent_page_id = (
            os.environ.get("NOTION_PARENT_PAGE_ID")
            or os.environ.get("NOTION_TEMPLATE_PAGE_ID")
            or ""
        )

        # Slack destination -- a channel name (e.g. "#general") resolved to an
        # ID via slackmcp_slack_search_channels, or a literal channel ID used
        # as-is. This is a shared team channel, not a DM: every new hire's
        # welcome message posts here.
        self.slack_welcome_channel = os.environ.get("SLACK_WELCOME_CHANNEL", "#general")

        # Domain used to construct the new hire's Google Workspace email
        # address (e.g. "yourcompany.com" -> "jane.doe@yourcompany.com").
        # Only used by the Google Workspace provisioning step.
        self.google_workspace_domain = os.environ.get("GOOGLE_WORKSPACE_DOMAIN", "")

        # How far back to consider a Gusto employee record a "new hire" when
        # scanning, based on start_date proximity to today. A generous window
        # by default since Gusto onboarding/paperwork can lag the actual
        # record-creation date by days to a couple of weeks.
        self.new_hire_lookback_days = self._parse_int("NEW_HIRE_LOOKBACK_DAYS", 14, min_value=0)
        self.new_hire_lookahead_days = self._parse_int("NEW_HIRE_LOOKAHEAD_DAYS", 30, min_value=0)

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

        if not self.gusto_user:
            errors.append("GUSTO_USER")
        if not self.notion_user:
            errors.append("NOTION_USER")
        if not self.slack_user:
            errors.append("SLACK_USER")
        # google_workspace_user is intentionally NOT required: Google Workspace
        # provisioning is optional/conditional (see README), so an unset
        # GOOGLE_WORKSPACE_USER should not block the whole agent from running.

        if not self.hr_admin_email:
            errors.append("HR_ADMIN_EMAIL")
        if not self.notion_parent_page_id:
            errors.append("NOTION_PARENT_PAGE_ID (or NOTION_TEMPLATE_PAGE_ID)")

        if errors:
            msg = f"Missing required config: {', '.join(errors)}"
            if logger:
                logger.error(msg)
            else:
                print(f"ERROR: {msg}")
            sys.exit(1)

    def get_connector_users(self) -> Dict[str, str]:
        """Mapping of connector name -> identifier, for the Step 0 auth-check loop.

        Google Workspace is included here too (it's harmless to attempt the
        check; run_flow.py treats a failed/absent result for this one
        connector as a warning, not a fatal error -- see main()).
        """
        users = {
            self.gusto_connector: self.gusto_user,
            self.notion_connector: self.notion_user,
            self.slack_connector: self.slack_user,
        }
        if self.google_workspace_user:
            users[self.google_workspace_connector] = self.google_workspace_user
        return users

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
