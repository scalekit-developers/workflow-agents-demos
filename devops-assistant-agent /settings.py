import json
import logging
import os
from typing import Dict

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("poller")


class Settings:
    SCALEKIT_ENV_URL: str = os.getenv("SCALEKIT_ENV_URL", "")
    SCALEKIT_CLIENT_ID: str = os.getenv("SCALEKIT_CLIENT_ID", "")
    SCALEKIT_CLIENT_SECRET: str = os.getenv("SCALEKIT_CLIENT_SECRET", "")

    # Scalekit connection identifiers (user-level identifier passed to execute_tool)
    GITHUB_IDENTIFIER: str = os.getenv("GITHUB_IDENTIFIER", "")
    LINEAR_IDENTIFIER: str = os.getenv("LINEAR_IDENTIFIER", "")
    SLACK_IDENTIFIER: str = os.getenv("SLACK_IDENTIFIER", "")

    # Scalekit connection names — pin to a specific connector so the right
    # credential is used when multiple accounts share the same identifier.
    # Find yours: Scalekit dashboard → Connected Accounts → connector column.
    GITHUB_CONNECTION_NAME: str = os.getenv("GITHUB_CONNECTION_NAME", "")
    LINEAR_CONNECTION_NAME: str = os.getenv("LINEAR_CONNECTION_NAME", "")
    SLACK_CONNECTION_NAME: str = os.getenv("SLACK_CONNECTION_NAME", "")

    GITHUB_REPO_OWNER: str = os.getenv("GITHUB_REPO_OWNER", "")
    GITHUB_REPO_NAME: str = os.getenv("GITHUB_REPO_NAME", "")

    SLACK_DIGEST_CHANNEL_ID: str = os.getenv("SLACK_DIGEST_CHANNEL_ID", "")

    LINEAR_TEAM_ID: str = os.getenv("LINEAR_TEAM_ID", "")
    LABEL_TO_LINEAR_TEAM_RAW: str = os.getenv("LABEL_TO_LINEAR_TEAM", "{}")
    try:
        LABEL_TO_LINEAR_TEAM: Dict[str, str] = json.loads(LABEL_TO_LINEAR_TEAM_RAW)
    except json.JSONDecodeError:
        LABEL_TO_LINEAR_TEAM = {}

    DIGEST_STALE_DAYS: int = int(os.getenv("DIGEST_STALE_DAYS", "5"))

    RETRY_ATTEMPTS: int = int(os.getenv("RETRY_ATTEMPTS", "3"))
    RETRY_BACKOFF: int = int(os.getenv("RETRY_BACKOFF", "1"))

    @classmethod
    def validate(cls) -> None:
        """Raise ValueError listing all missing required env vars before any API call is made."""
        required = {
            "SCALEKIT_ENV_URL": cls.SCALEKIT_ENV_URL,
            "SCALEKIT_CLIENT_ID": cls.SCALEKIT_CLIENT_ID,
            "SCALEKIT_CLIENT_SECRET": cls.SCALEKIT_CLIENT_SECRET,
            "GITHUB_IDENTIFIER": cls.GITHUB_IDENTIFIER,
            "LINEAR_IDENTIFIER": cls.LINEAR_IDENTIFIER,
            "SLACK_IDENTIFIER": cls.SLACK_IDENTIFIER,
            "GITHUB_REPO_OWNER": cls.GITHUB_REPO_OWNER,
            "GITHUB_REPO_NAME": cls.GITHUB_REPO_NAME,
            "SLACK_DIGEST_CHANNEL_ID": cls.SLACK_DIGEST_CHANNEL_ID,
            "LINEAR_TEAM_ID": cls.LINEAR_TEAM_ID,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ValueError(f"Missing required env vars: {', '.join(missing)}")


try:
    Settings.validate()
    log.debug("DevOps Assistant settings loaded successfully.")
except ValueError as e:
    log.warning("Settings validation: %s", e)
