"""Centralized configuration with fail-fast validation."""
import os
import json
from typing import Optional


class Settings:
    """Load and validate all environment variables at startup."""

    def __init__(self):
        self.SCALEKIT_ENV_URL = os.environ.get("SCALEKIT_ENV_URL")
        self.SCALEKIT_CLIENT_ID = os.environ.get("SCALEKIT_CLIENT_ID")
        self.SCALEKIT_CLIENT_SECRET = os.environ.get("SCALEKIT_CLIENT_SECRET")

        self.GRANOLA_USER = os.environ.get("GRANOLA_USER")
        self.HUBSPOT_USER = os.environ.get("HUBSPOT_USER")
        self.GMAIL_USER = os.environ.get("GMAIL_USER")
        self.SLACK_USER = os.environ.get("SLACK_USER")

        self.SLACK_CONNECTOR = os.environ.get("SLACK_CONNECTOR", "slack")
        self.SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL")

        self.OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
        self.OPENROUTER_MODEL = os.environ.get(
            "OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free"
        )

        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

    def validate(self) -> None:
        """Fail fast if required vars are missing."""
        missing = []

        # Required Scalekit
        if not self.SCALEKIT_ENV_URL:
            missing.append("SCALEKIT_ENV_URL")
        if not self.SCALEKIT_CLIENT_ID:
            missing.append("SCALEKIT_CLIENT_ID")
        if not self.SCALEKIT_CLIENT_SECRET:
            missing.append("SCALEKIT_CLIENT_SECRET")

        # Required identifiers
        if not self.GRANOLA_USER:
            missing.append("GRANOLA_USER")
        if not self.HUBSPOT_USER:
            missing.append("HUBSPOT_USER")
        if not self.GMAIL_USER:
            missing.append("GMAIL_USER")
        if not self.SLACK_USER:
            missing.append("SLACK_USER")

        # Required Slack
        if not self.SLACK_CHANNEL:
            missing.append("SLACK_CHANNEL")

        if missing:
            raise ValueError(
                f"Missing required env vars:\n"
                f"  {', '.join(missing)}\n"
                f"Copy .env.example to .env and fill in all values."
            )


# Global instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get or create global settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.validate()
    return _settings
