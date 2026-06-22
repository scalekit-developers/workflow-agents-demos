import os
from dotenv import load_dotenv

load_dotenv()

# Scalekit credentials
SCALEKIT_ENV_URL = os.getenv("SCALEKIT_ENV_URL")
SCALEKIT_CLIENT_ID = os.getenv("SCALEKIT_CLIENT_ID")
SCALEKIT_CLIENT_SECRET = os.getenv("SCALEKIT_CLIENT_SECRET")

# Connected account identifiers
ZENDESK_IDENTIFIER = os.getenv("ZENDESK_IDENTIFIER")
SLACK_IDENTIFIER = os.getenv("SLACK_IDENTIFIER")
NOTION_IDENTIFIER = os.getenv("NOTION_IDENTIFIER")

# Connection name pinning (recommended — prevents ambiguity when one identifier
# has multiple connections for the same service)
ZENDESK_CONNECTION_NAME = os.getenv("ZENDESK_CONNECTION_NAME")
SLACK_CONNECTION_NAME = os.getenv("SLACK_CONNECTION_NAME")
NOTION_CONNECTION_NAME = os.getenv("NOTION_CONNECTION_NAME")

# Application settings
SLACK_SUPPORT_CHANNEL = os.getenv("SLACK_SUPPORT_CHANNEL")
NOTION_KB_DATABASE_ID = os.getenv("NOTION_KB_DATABASE_ID")

# Google / Gemini key for ADK
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_ADK_API_KEY")

# Optional tuning
GOOGLE_ADK_MODEL = os.getenv("GOOGLE_ADK_MODEL", "gemini-2.0-flash")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


def validate() -> None:
    """Raise ValueError listing all missing required env vars before any API call is made."""
    required = [
        ("SCALEKIT_ENV_URL", SCALEKIT_ENV_URL),
        ("SCALEKIT_CLIENT_ID", SCALEKIT_CLIENT_ID),
        ("SCALEKIT_CLIENT_SECRET", SCALEKIT_CLIENT_SECRET),
        ("ZENDESK_IDENTIFIER", ZENDESK_IDENTIFIER),
        ("SLACK_IDENTIFIER", SLACK_IDENTIFIER),
        ("NOTION_IDENTIFIER", NOTION_IDENTIFIER),
        ("SLACK_SUPPORT_CHANNEL", SLACK_SUPPORT_CHANNEL),
        ("NOTION_KB_DATABASE_ID", NOTION_KB_DATABASE_ID),
        ("GOOGLE_API_KEY (or GOOGLE_ADK_API_KEY)", GOOGLE_API_KEY),
    ]
    missing = [name for name, val in required if not val]
    if missing:
        raise ValueError(f"Missing required env vars: {', '.join(missing)}")
