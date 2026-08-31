"""
Configuration management with validation.

All settings loaded from environment variables.
Validates on startup and provides clear error messages.
"""

import os
import sys
from typing import Dict, Optional

logger = None  # Set by run_flow after logging is initialized

# Structured, extensible set of supported change types. "payroll or bank
# detail change" in the brief implies more than just direct deposit, so this
# is a set the agent validates against rather than hardcoding a single field
# -- adding a new supported change type later means adding a label here and
# a validation rule in aggregator.py's validate_new_value(), not restructuring
# config.py.
SUPPORTED_CHANGE_TYPES = ("bank_account", "routing_number", "pay_rate", "compensation")


class Config:
    """Application configuration."""

    def __init__(self):
        """Load configuration from environment variables."""
        # Scalekit
        self.scalekit_env_url = os.environ.get("SCALEKIT_ENV_URL")
        self.scalekit_client_id = os.environ.get("SCALEKIT_CLIENT_ID")
        self.scalekit_client_secret = os.environ.get("SCALEKIT_CLIENT_SECRET")

        # Connector identities (the "identifier" each connected account is keyed by).
        # This agent runs ON BEHALF OF the employee, but Scalekit's connected
        # accounts in this workspace are keyed by the People Ops operator's own
        # email for each connector (the operator's Gusto/Slack/Sheets access is
        # what the agent actually uses -- see EMPLOYEE_EMAIL below for whose
        # RECORD is being acted on, which is a separate concept).
        self.gusto_user = os.environ.get("GUSTO_USER")
        self.slack_user = os.environ.get("SLACK_USER")
        self.google_sheets_user = os.environ.get("GOOGLE_SHEETS_USER")

        # Connector names -- must match the exact connection name shown in the
        # Scalekit dashboard, which is often auto-suffixed per workspace
        # (e.g. "gustomcp-SoSOMZ20"), not just the generic provider label.
        # Verified live against this workspace's connections.
        self.gusto_connector = os.environ.get("GUSTO_CONNECTOR", "gustomcp-SoSOMZ20")
        self.slack_connector = os.environ.get("SLACK_CONNECTOR", "slackmcp")
        self.google_sheets_connector = os.environ.get("GOOGLE_SHEETS_CONNECTOR", "googlesheets-BOzvgKS0")

        # The employee whose payroll/bank record this run is about (the
        # delegated identity). Not necessarily the same as any *_USER above --
        # those identify which connected Scalekit accounts perform the API
        # calls; EMPLOYEE_EMAIL/EMPLOYEE_GUSTO_ID identify WHOSE Gusto record
        # is being read and changed, and where the Slack confirmation DM goes.
        self.employee_email = os.environ.get("EMPLOYEE_EMAIL")
        self.employee_gusto_id = os.environ.get("EMPLOYEE_GUSTO_ID", "")  # optional: skips the email->UUID lookup if set
        self.employee_record_type = os.environ.get("EMPLOYEE_RECORD_TYPE", "").strip().lower()  # "employee" or "contractor", optional hint
        self.employee_slack_id = os.environ.get("EMPLOYEE_SLACK_ID", "")  # optional: skips the email->Slack-ID lookup if set

        # The change being requested. Structured and extensible: CHANGE_TYPE
        # must be one of SUPPORTED_CHANGE_TYPES, and NEW_VALUE is validated
        # against it in aggregator.py before any submission is attempted.
        self.change_type = os.environ.get("CHANGE_TYPE", "").strip().lower()
        self.new_value = os.environ.get("NEW_VALUE", "")

        # Google Sheets destination -- the spreadsheet must already exist
        # (no create-spreadsheet-from-scratch step in the normal flow; see
        # provisioning.py). The tab/sheet name is auto-created if missing.
        self.google_sheets_spreadsheet_id = os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID")
        self.google_sheets_tab_name = os.environ.get("GOOGLE_SHEETS_TAB_NAME", "Payroll Change Log")

        # Logging
        self.log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

        # Allows a deliberate, explicit simulated/dry-run mode for exercising
        # the full pipeline (masking, idempotency, Sheets logging, Slack
        # confirmation) without calling Gusto's (currently nonexistent, see
        # connectors.py) write tool. Distinct from "no write tool exists" --
        # this flag lets an operator ALSO simulate on a hypothetical future
        # environment where a write tool does exist, without risking a real
        # submission. Defaults to false so a bare `python run_flow.py` never
        # silently no-ops the write step without saying so loudly.
        self.simulate_submission = os.environ.get("SIMULATE_SUBMISSION", "false").lower() == "true"

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
        if not self.slack_user:
            errors.append("SLACK_USER")
        if not self.google_sheets_user:
            errors.append("GOOGLE_SHEETS_USER")

        if not self.employee_email:
            errors.append("EMPLOYEE_EMAIL")
        if not self.google_sheets_spreadsheet_id:
            errors.append("GOOGLE_SHEETS_SPREADSHEET_ID")

        if not self.change_type:
            errors.append("CHANGE_TYPE")
        elif self.change_type not in SUPPORTED_CHANGE_TYPES:
            errors.append(
                f"CHANGE_TYPE (must be one of {', '.join(SUPPORTED_CHANGE_TYPES)}, got {self.change_type!r})"
            )

        if not self.new_value:
            errors.append("NEW_VALUE")

        if errors:
            msg = f"Missing or invalid required config: {', '.join(errors)}"
            if logger:
                logger.error(msg)
            else:
                print(f"ERROR: {msg}")
            sys.exit(1)

    def get_connector_users(self) -> Dict[str, str]:
        """Mapping of connector name -> identifier, for auth checks."""
        return {
            self.gusto_connector: self.gusto_user,
            self.slack_connector: self.slack_user,
            self.google_sheets_connector: self.google_sheets_user,
        }

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
