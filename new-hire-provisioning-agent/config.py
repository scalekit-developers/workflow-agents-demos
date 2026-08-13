"""
Configuration management with validation.

All settings loaded from environment variables.
Validates on startup and provides clear error messages.
"""

import datetime
import logging
import os
import sys
from typing import List, Optional

logger = logging.getLogger(__name__)


class Config:
    """Application configuration."""

    def __init__(self):
        """Load configuration from environment variables."""
        # Scalekit
        self.scalekit_env_url = os.environ.get("SCALEKIT_ENV_URL")
        self.scalekit_client_id = os.environ.get("SCALEKIT_CLIENT_ID")
        self.scalekit_client_secret = os.environ.get("SCALEKIT_CLIENT_SECRET")

        # Connector identities (the "identifier" each connected account is keyed by)
        self.deel_user = os.environ.get("DEEL_USER")
        self.notion_user = os.environ.get("NOTION_USER")
        self.slack_user = os.environ.get("SLACK_USER")
        self.google_workspace_user = os.environ.get("GOOGLE_WORKSPACE_USER")

        # Connector names -- must match the exact connection name shown in the
        # Scalekit dashboard, which is often auto-suffixed per workspace
        # (e.g. "deelmcp-zTWsHKTh", "notionmcp-chAb8Lfz"), not just the
        # generic provider label.
        self.deel_connector = os.environ.get("DEEL_CONNECTOR", "deelmcp-zTWsHKTh")
        self.notion_connector = os.environ.get("NOTION_CONNECTOR", "notionmcp-chAb8Lfz")
        self.slack_connector = os.environ.get("SLACK_CONNECTOR", "slackmcp")

        # Google Workspace via Domain-Wide Delegation. Shows setup:
        # not_configured in most workspaces until an admin completes the GCP
        # service account + DWD setup (see README Prerequisites) and connects
        # it in the Scalekit dashboard. Until then, this agent treats it as
        # "not authorized yet" rather than a crash -- see connectors.py
        # GoogleWorkspaceConnector and run_flow.py Step 3.
        self.google_workspace_connector = os.environ.get("GOOGLE_WORKSPACE_CONNECTOR", "googledwd")

        # HR admin whose identity this agent runs on behalf of. Used only as a
        # label in logs/state.
        self.hr_admin_email = os.environ.get("HR_ADMIN_EMAIL")

        # The new hire's real details. Deel's catalog has no "list of pending
        # hires waiting to be onboarded" concept -- every real Deel listing
        # tool lists people already fully in the system -- so unlike the
        # Gusto-based version of this agent (which scanned for existing
        # records), this agent takes the new hire's details directly as
        # input and creates them for real in Deel. One run provisions one
        # new hire, matching the sibling pto-leave-request-agent's
        # one-request-per-run shape.
        self.new_hire_first_name = os.environ.get("NEW_HIRE_FIRST_NAME", "")
        self.new_hire_last_name = os.environ.get("NEW_HIRE_LAST_NAME", "")
        self.new_hire_personal_email = os.environ.get("NEW_HIRE_PERSONAL_EMAIL", "")
        self.new_hire_work_email = os.environ.get("NEW_HIRE_WORK_EMAIL", "")
        self.new_hire_country = os.environ.get("NEW_HIRE_COUNTRY", "").strip().upper()
        self.new_hire_state = os.environ.get("NEW_HIRE_STATE", "").strip().upper()
        self.new_hire_nationality = os.environ.get("NEW_HIRE_NATIONALITY", "").strip().upper()
        self.new_hire_job_title = os.environ.get("NEW_HIRE_JOB_TITLE", "")
        self.new_hire_seniority = os.environ.get("NEW_HIRE_SENIORITY", "")
        self.new_hire_start_date = os.environ.get("NEW_HIRE_START_DATE", "")
        self.new_hire_salary = os.environ.get("NEW_HIRE_SALARY", "")
        self.new_hire_currency = os.environ.get("NEW_HIRE_CURRENCY", "").strip().upper()
        self.new_hire_employment_type = os.environ.get("NEW_HIRE_EMPLOYMENT_TYPE", "FULL_TIME").strip().upper()

        # Optional: skip these to auto-resolve at startup if exactly one
        # legal entity / team exists in the connected Deel account (see
        # provisioning.py resolve_deel_legal_entity/resolve_deel_team).
        self.deel_legal_entity_id = os.environ.get("DEEL_LEGAL_ENTITY_ID", "")
        self.deel_team_id = os.environ.get("DEEL_TEAM_ID", "")
        self.deel_department_id = os.environ.get("DEEL_DEPARTMENT_ID", "")

        # Notion destination -- an existing page under which onboarding docs
        # get created as child pages.
        self.notion_parent_page_id = (
            os.environ.get("NOTION_PARENT_PAGE_ID")
            or os.environ.get("NOTION_TEMPLATE_PAGE_ID")
            or ""
        )

        # Slack destination -- a channel name (e.g. "#general") resolved to an
        # ID via slackmcp_slack_search_channels, or a literal channel ID used
        # as-is. This is a shared team channel, not a DM.
        self.slack_welcome_channel = os.environ.get("SLACK_WELCOME_CHANNEL", "#general")

        # Domain used to construct the new hire's Google Workspace email
        # address once GOOGLEDWD is implemented. Only used by that step.
        self.google_workspace_domain = os.environ.get("GOOGLE_WORKSPACE_DOMAIN", "")

        # If true, resolve everything (legal entity, team, seniority) and log
        # exactly what WOULD be created, but never call
        # deelmcp_org_direct_employee_create. Recommended for first-time
        # setup verification, since Deel's catalog has no delete/terminate
        # tool for a direct employee (confirmed live, see connectors.py) --
        # a mistaken real creation cannot be undone through this agent or any
        # other Scalekit tool.
        self.new_hire_dry_run = os.environ.get("NEW_HIRE_DRY_RUN", "false").lower() == "true"

        # Timing / mode. Provisioning one hire is inherently a one-shot
        # action once completed; POLLING_MODE here re-checks whether the
        # CONFIGURED hire has finished processing yet, rather than
        # re-creating a completed hire on a timer (see state.py and
        # run_flow.py).
        self.polling_mode = os.environ.get("POLLING_MODE", "false").lower() == "true"
        self.poll_interval_minutes = self._parse_int("POLL_INTERVAL_MINUTES", 15, min_value=1)
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

        if not self.deel_user:
            errors.append("DEEL_USER")
        if not self.notion_user:
            errors.append("NOTION_USER")
        if not self.slack_user:
            errors.append("SLACK_USER")
        # google_workspace_user is intentionally NOT required: Google
        # Workspace provisioning is optional/conditional (see README).

        if not self.hr_admin_email:
            errors.append("HR_ADMIN_EMAIL")
        if not self.notion_parent_page_id:
            errors.append("NOTION_PARENT_PAGE_ID (or NOTION_TEMPLATE_PAGE_ID)")

        if not self.new_hire_first_name:
            errors.append("NEW_HIRE_FIRST_NAME")
        if not self.new_hire_last_name:
            errors.append("NEW_HIRE_LAST_NAME")
        if not self.new_hire_personal_email:
            errors.append("NEW_HIRE_PERSONAL_EMAIL")
        if not self.new_hire_country:
            errors.append("NEW_HIRE_COUNTRY")
        if not self.new_hire_nationality:
            errors.append("NEW_HIRE_NATIONALITY")
        if not self.new_hire_job_title:
            errors.append("NEW_HIRE_JOB_TITLE")
        if not self.new_hire_seniority:
            errors.append("NEW_HIRE_SENIORITY")
        if not self.new_hire_start_date:
            errors.append("NEW_HIRE_START_DATE")
        if not self.new_hire_salary:
            errors.append("NEW_HIRE_SALARY")
        if not self.new_hire_currency:
            errors.append("NEW_HIRE_CURRENCY")

        if errors:
            msg = f"Missing required config: {', '.join(errors)}"
            logger.error(msg)
            sys.exit(1)

        if self.new_hire_employment_type not in ("FULL_TIME", "PART_TIME"):
            msg = f"Invalid NEW_HIRE_EMPLOYMENT_TYPE: {self.new_hire_employment_type!r}. Must be FULL_TIME or PART_TIME"
            logger.error(msg)
            sys.exit(1)

        self._validate_salary()
        self._validate_start_date()

    def _validate_salary(self):
        try:
            value = float(self.new_hire_salary)
        except ValueError:
            self._fatal(f"Invalid NEW_HIRE_SALARY: {self.new_hire_salary!r} (must be a number)")
            return
        if value <= 0:
            self._fatal(f"NEW_HIRE_SALARY must be greater than 0, got {value:g}")
            return
        self.new_hire_salary = value

    def _validate_start_date(self):
        try:
            datetime.date.fromisoformat(self.new_hire_start_date)
        except ValueError:
            self._fatal(f"Invalid NEW_HIRE_START_DATE: {self.new_hire_start_date!r} (expected YYYY-MM-DD)")

    def _fatal(self, msg: str):
        logger.error(msg)
        sys.exit(1)

    def get_connector_users(self) -> List[tuple]:
        """
        List of (connector name, identifier) pairs, for the Step 0
        auth-check loop. Google Workspace is included too when configured
        (it's harmless to attempt the check; run_flow.py treats a
        failed/absent result for this one connector as a warning, not a
        fatal error -- see main()).
        """
        users = [
            (self.deel_connector, self.deel_user),
            (self.notion_connector, self.notion_user),
            (self.slack_connector, self.slack_user),
        ]
        if self.google_workspace_user:
            users.append((self.google_workspace_connector, self.google_workspace_user))
        return users

    @staticmethod
    def _parse_int(key: str, default: int, min_value: Optional[int] = None) -> int:
        raw = os.environ.get(key, str(default))
        try:
            value = int(raw)
        except ValueError:
            msg = f"Invalid {key}: {raw!r} (must be an integer)"
            logger.error(msg)
            sys.exit(1)

        if min_value is not None and value < min_value:
            msg = f"{key} must be >= {min_value}, got {value}"
            logger.error(msg)
            sys.exit(1)

        return value
