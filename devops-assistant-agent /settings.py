import os
import json
from typing import Dict, List, Optional
from dotenv import load_dotenv

load_dotenv()

class Settings:
    SCALEKIT_ENV_URL: str = os.getenv("SCALEKIT_ENV_URL", "")
    SCALEKIT_CLIENT_ID: str = os.getenv("SCALEKIT_CLIENT_ID", "")
    SCALEKIT_CLIENT_SECRET: str = os.getenv("SCALEKIT_CLIENT_SECRET", "")

    GITHUB_REPO_OWNER: str = os.getenv("GITHUB_REPO_OWNER", "")
    GITHUB_REPO_NAME: str = os.getenv("GITHUB_REPO_NAME", "")
    WEBHOOK_SECRET: Optional[str] = os.getenv("WEBHOOK_SECRET")

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
        required = {
            "SCALEKIT_ENV_URL": cls.SCALEKIT_ENV_URL,
            "SCALEKIT_CLIENT_ID": cls.SCALEKIT_CLIENT_ID,
            "SCALEKIT_CLIENT_SECRET": cls.SCALEKIT_CLIENT_SECRET,
            "GITHUB_REPO_OWNER": cls.GITHUB_REPO_OWNER,
            "GITHUB_REPO_NAME": cls.GITHUB_REPO_NAME,
            "SLACK_DIGEST_CHANNEL_ID": cls.SLACK_DIGEST_CHANNEL_ID,
            "LINEAR_TEAM_ID": cls.LINEAR_TEAM_ID,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ValueError(f"Missing required env: {', '.join(missing)}")

try:
    Settings.validate()
    print("✅ DevOps Assistant settings loaded")
except Exception as e:
    print(f"⚠️ Settings error: {e}")
