"""Centralized configuration with fail-fast validation."""
import os
from typing import Optional


class Settings:
    """Load and validate all environment variables at startup."""

    def __init__(self):
        self.SCALEKIT_ENV_URL = os.environ.get("SCALEKIT_ENV_URL")
        self.SCALEKIT_CLIENT_ID = os.environ.get("SCALEKIT_CLIENT_ID")
        self.SCALEKIT_CLIENT_SECRET = os.environ.get("SCALEKIT_CLIENT_SECRET")

        self.GONG_USER = os.environ.get("GONG_USER")
        self.ATTIO_USER = os.environ.get("ATTIO_USER")
        self.SLACK_USER = os.environ.get("SLACK_USER")

        self.GONG_CONNECTOR = os.environ.get("GONG_CONNECTOR", "gong")
        self.ATTIO_CONNECTOR = os.environ.get("ATTIO_CONNECTOR", "attio")
        self.SLACK_CONNECTOR = os.environ.get("SLACK_CONNECTOR", "slack")

        self.SLACK_DM_USER = os.environ.get("SLACK_DM_USER")

        self.OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
        self.OPENROUTER_MODEL = os.environ.get(
            "OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free"
        )

        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

    def validate(self) -> None:
        """Fail fast if required vars are missing."""
        missing = []

        if not self.SCALEKIT_ENV_URL:
            missing.append("SCALEKIT_ENV_URL")
        if not self.SCALEKIT_CLIENT_ID:
            missing.append("SCALEKIT_CLIENT_ID")
        if not self.SCALEKIT_CLIENT_SECRET:
            missing.append("SCALEKIT_CLIENT_SECRET")

        if not self.GONG_USER:
            missing.append("GONG_USER")
        if not self.ATTIO_USER:
            missing.append("ATTIO_USER")
        if not self.SLACK_USER:
            missing.append("SLACK_USER")

        if not self.SLACK_DM_USER:
            missing.append("SLACK_DM_USER")

        if missing:
            raise ValueError(
                f"Missing required env vars:\n"
                f"  {', '.join(missing)}\n"
                f"Copy .env.example to .env and fill in all values."
            )


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get or create global settings instance."""
    global _settings
    if _settings is None:
        instance = Settings()
        instance.validate()
        _settings = instance
    return _settings
