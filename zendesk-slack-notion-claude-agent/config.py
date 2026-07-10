"""
Configuration management with validation.

All settings loaded from environment variables.
Validates on startup and provides clear error messages.
"""

import os
import sys
from typing import Dict

logger = None  # Set by main after logging is initialized

class Config:
    """Application configuration."""

    def __init__(self):
        """Load and validate configuration from environment variables."""
        self.scalekit_env_url = os.environ.get("SCALEKIT_ENV_URL")
        self.scalekit_client_id = os.environ.get("SCALEKIT_CLIENT_ID")
        self.scalekit_client_secret = os.environ.get("SCALEKIT_CLIENT_SECRET")

        self.zendesk_user = os.environ.get("ZENDESK_USER")
        self.slack_user = os.environ.get("SLACK_USER")
        self.notion_user = os.environ.get("NOTION_USER")

        self.slack_connector = os.environ.get("SLACK_CONNECTOR", "slack")
        self.notion_db_id = os.environ.get("NOTION_DB_ID", "")
        self.support_email = os.environ.get("SUPPORT_EMAIL", self.zendesk_user)

        self.polling_mode = os.environ.get("POLLING_MODE", "false").lower() == "true"
        self.poll_interval_minutes = self._parse_int("POLL_INTERVAL_MINUTES", 2, min_value=1)
        self.log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

        self.openrouter_api_key = os.environ.get("OPENROUTER_API_KEY", "")
        self.openrouter_model = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")

        self.channel_map = {
            "bug": os.environ.get("CHANNEL_BUG", "#engineering"),
            "billing": os.environ.get("CHANNEL_BILLING", "#billing"),
            "feature_request": os.environ.get("CHANNEL_FEATURE", "#product-feedback"),
            "how_to": os.environ.get("CHANNEL_HOWTO", "#support-triage"),
            "account_issue": os.environ.get("CHANNEL_ACCOUNT", "#support-triage"),
        }
        self.fallback_channel = os.environ.get("FALLBACK_CHANNEL", "#support-triage")

        self.severity_priority = {
            "P0": "urgent",
            "P1": "high",
            "P2": "normal",
            "P3": "low",
        }

    def validate(self):
        """Validate required configuration. Fails fast if missing."""
        errors = []

        # Scalekit
        if not self.scalekit_env_url:
            errors.append("SCALEKIT_ENV_URL")
        if not self.scalekit_client_id:
            errors.append("SCALEKIT_CLIENT_ID")
        if not self.scalekit_client_secret:
            errors.append("SCALEKIT_CLIENT_SECRET")

        # Connectors
        if not self.zendesk_user:
            errors.append("ZENDESK_USER")
        if not self.slack_user:
            errors.append("SLACK_USER")
        if not self.notion_user:
            errors.append("NOTION_USER")

        if errors:
            if logger:
                logger.error(f"Missing required config: {', '.join(errors)}")
            else:
                print(f"ERROR: Missing required config: {', '.join(errors)}")
            sys.exit(1)

    def get_connector_users(self) -> Dict[str, str]:
        """Get mapping of connector names to user identifiers."""
        return {
            "zendesk": self.zendesk_user,
            self.slack_connector: self.slack_user,
            "notion": self.notion_user,
        }

    @staticmethod
    def _parse_int(key: str, default: int, min_value: int = None) -> int:
        """Parse integer from environment variable."""
        try:
            value = int(os.environ.get(key, default))
            if min_value is not None and value < min_value:
                if logger:
                    logger.error(f"{key} must be >= {min_value}, got {value}")
                else:
                    print(f"ERROR: {key} must be >= {min_value}, got {value}")
                sys.exit(1)
            return value
        except ValueError:
            if logger:
                logger.error(f"Invalid {key}: {os.environ.get(key)}")
            else:
                print(f"ERROR: Invalid {key}: {os.environ.get(key)}")
            sys.exit(1)
