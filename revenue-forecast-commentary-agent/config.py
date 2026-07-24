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
        self.salesforce_user = os.environ.get("SALESFORCE_USER")
        self.hubspot_user = os.environ.get("HUBSPOT_USER")
        self.slack_user = os.environ.get("SLACK_USER")
        self.google_sheets_user = os.environ.get("GOOGLE_SHEETS_USER")

        # Connector names -- must match the exact connection name shown in the
        # Scalekit dashboard, which is often auto-suffixed per workspace
        # (e.g. "salesforce-1", "googlesheets-BOzvgKS0"), not just the generic
        # provider label. Verified live against this workspace's connections.
        self.salesforce_connector = os.environ.get("SALESFORCE_CONNECTOR", "salesforce-1")
        self.hubspot_connector = os.environ.get("HUBSPOT_CONNECTOR", "hubspot")
        self.slack_connector = os.environ.get("SLACK_CONNECTOR", "slackmcp")
        self.google_sheets_connector = os.environ.get("GOOGLE_SHEETS_CONNECTOR", "googlesheets-BOzvgKS0")

        # Analyst running this cycle -- used only as a label in commentary/state,
        # since Salesforce/HubSpot pipeline queries here are org-wide (open pipeline),
        # not scoped to a single owner/rep.
        self.analyst_email = os.environ.get("ANALYST_EMAIL")

        # Forecast period label (e.g. "2026-W30" or "Q3 2026") -- a human-
        # readable label shown in commentary and logged as a Google Sheets
        # column. Does NOT gate whether Slack gets a post: that's decided by
        # whether the pipeline content actually changed since the last post
        # (see state.py's fingerprint-based change detection).
        self.forecast_period = os.environ.get("FORECAST_PERIOD", "")

        # Slack destination -- a channel name (e.g. "#revenue-ops") that gets
        # resolved to an ID via slackmcp_slack_search_channels, or a literal
        # channel/user ID (C..., D..., U...) used as-is.
        self.slack_channel = os.environ.get("SLACK_CHANNEL", "#revenue-ops")

        # Google Sheets destination -- the spreadsheet must already exist
        # (no create-spreadsheet-from-scratch step in the normal flow; see
        # provisioning.py). The tab/sheet name is auto-created if missing.
        self.google_sheets_spreadsheet_id = os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID")
        self.google_sheets_tab_name = os.environ.get("GOOGLE_SHEETS_TAB_NAME", "Forecast Log")

        # Coverage ratio target -- open pipeline value / quota below this
        # multiple is flagged "at risk". 3x is a common SaaS sales-planning
        # rule of thumb (see aggregator.py docstring for the full formula).
        self.coverage_ratio_target = self._parse_float("COVERAGE_RATIO_TARGET", 3.0, min_value=0.1)

        # Quota this cycle's pipeline is measured against. There is no
        # authoritative "quota" object available from either Salesforce or
        # HubSpot via the connected tools, so this is a configured number
        # (e.g. the team's quarterly or monthly target in the same currency
        # as Amount/amount fields).
        self.quota_target = self._parse_float("QUOTA_TARGET", 100000.0, min_value=0.0)

        # Stages considered "open" (not closed) if IsClosed/isClosed metadata
        # is unavailable for a given record. Used as a fallback signal only.
        self.stale_deal_days = self._parse_int("STALE_DEAL_DAYS", 90, min_value=1)

        # Timing / mode
        self.polling_mode = os.environ.get("POLLING_MODE", "false").lower() == "true"
        self.poll_interval_minutes = self._parse_int("POLL_INTERVAL_MINUTES", 10080, min_value=1)  # weekly default
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

        if not self.salesforce_user:
            errors.append("SALESFORCE_USER")
        if not self.hubspot_user:
            errors.append("HUBSPOT_USER")
        if not self.slack_user:
            errors.append("SLACK_USER")
        if not self.google_sheets_user:
            errors.append("GOOGLE_SHEETS_USER")

        if not self.analyst_email:
            errors.append("ANALYST_EMAIL")
        if not self.google_sheets_spreadsheet_id:
            errors.append("GOOGLE_SHEETS_SPREADSHEET_ID")

        if errors:
            msg = f"Missing required config: {', '.join(errors)}"
            if logger:
                logger.error(msg)
            else:
                print(f"ERROR: {msg}")
            sys.exit(1)

        if not self.forecast_period:
            # Not fatal -- default to the current ISO week so re-runs within
            # the same week are naturally deduped by the state guard.
            import datetime

            iso = datetime.date.today().isocalendar()
            self.forecast_period = f"{iso[0]}-W{iso[1]:02d}"

    def get_connector_users(self) -> Dict[str, str]:
        """Mapping of connector name -> identifier, for auth checks."""
        return {
            self.salesforce_connector: self.salesforce_user,
            self.hubspot_connector: self.hubspot_user,
            self.slack_connector: self.slack_user,
            self.google_sheets_connector: self.google_sheets_user,
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

    @staticmethod
    def _parse_float(key: str, default: float, min_value: float = None) -> float:
        raw = os.environ.get(key, str(default))
        try:
            value = float(raw)
        except ValueError:
            msg = f"Invalid {key}: {raw!r} (must be a number)"
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
