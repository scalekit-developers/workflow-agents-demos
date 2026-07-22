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
        self.airtable_user = os.environ.get("AIRTABLE_USER")
        self.google_forms_user = os.environ.get("GOOGLE_FORMS_USER")
        self.notion_user = os.environ.get("NOTION_USER")
        self.slack_user = os.environ.get("SLACK_USER")

        # Connector names -- must match the exact connection name shown in the
        # Scalekit dashboard, which is often auto-suffixed per workspace
        # (e.g. "airtable-3j16TKTG"), not just the generic provider label.
        self.airtable_connector = os.environ.get("AIRTABLE_CONNECTOR", "airtable")
        self.google_forms_connector = os.environ.get("GOOGLE_FORMS_CONNECTOR", "googleforms")
        self.notion_connector = os.environ.get("NOTION_CONNECTOR", "NOTIONMCP")
        self.slack_connector = os.environ.get("SLACK_CONNECTOR", "slackmcp")

        # Manager running this cycle -- used to scope direct reports
        self.manager_email = os.environ.get("MANAGER_EMAIL")
        self.manager_slack_id = os.environ.get("MANAGER_SLACK_ID", self.manager_email)
        self.direct_reports = self._parse_list("DIRECT_REPORTS")

        # Airtable source
        self.airtable_base_id = os.environ.get("AIRTABLE_BASE_ID")
        self.airtable_table_name = os.environ.get("AIRTABLE_TABLE_NAME", "Performance Reviews")
        self.airtable_manager_field = os.environ.get("AIRTABLE_MANAGER_FIELD", "Manager Email")
        self.airtable_employee_field = os.environ.get("AIRTABLE_EMPLOYEE_FIELD", "Employee Name")
        self.airtable_view = os.environ.get("AIRTABLE_VIEW", "")

        # Google Forms source
        self.google_form_id = os.environ.get("GOOGLE_FORM_ID")
        self.form_employee_question_id = os.environ.get("FORM_EMPLOYEE_QUESTION_ID", "")

        # Notion destination
        self.notion_parent_page_id = os.environ.get("NOTION_PARENT_PAGE_ID")

        # Review period label (e.g. "Q2 2026")
        self.review_period = os.environ.get("REVIEW_PERIOD", "Current Cycle")

        # Timing / mode
        self.polling_mode = os.environ.get("POLLING_MODE", "false").lower() == "true"
        self.poll_interval_minutes = self._parse_int("POLL_INTERVAL_MINUTES", 60, min_value=1)
        self.log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

        # LLM (optional)
        self.openrouter_api_key = os.environ.get("OPENROUTER_API_KEY", "")
        self.openrouter_model = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")

    def validate(self):
        """Validate required configuration. Fails fast if missing."""
        errors = []

        if not self.scalekit_env_url:
            errors.append("SCALEKIT_ENV_URL")
        if not self.scalekit_client_id:
            errors.append("SCALEKIT_CLIENT_ID")
        if not self.scalekit_client_secret:
            errors.append("SCALEKIT_CLIENT_SECRET")

        if not self.airtable_user:
            errors.append("AIRTABLE_USER")
        if not self.google_forms_user:
            errors.append("GOOGLE_FORMS_USER")
        if not self.notion_user:
            errors.append("NOTION_USER")
        if not self.slack_user:
            errors.append("SLACK_USER")

        if not self.manager_email:
            errors.append("MANAGER_EMAIL")
        if not self.airtable_base_id:
            errors.append("AIRTABLE_BASE_ID")
        if not self.google_form_id:
            errors.append("GOOGLE_FORM_ID")
        if not self.notion_parent_page_id:
            errors.append("NOTION_PARENT_PAGE_ID")

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
            self.airtable_connector: self.airtable_user,
            self.google_forms_connector: self.google_forms_user,
            self.notion_connector: self.notion_user,
            self.slack_connector: self.slack_user,
        }

    @staticmethod
    def _parse_list(key: str) -> Optional[list]:
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
