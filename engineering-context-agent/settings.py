import os

from dotenv import load_dotenv

load_dotenv()

SCALEKIT_ENV_URL       = os.getenv("SCALEKIT_ENV_URL", "")
SCALEKIT_CLIENT_ID     = os.getenv("SCALEKIT_CLIENT_ID", "")
SCALEKIT_CLIENT_SECRET = os.getenv("SCALEKIT_CLIENT_SECRET", "")

# Connector names — pin to the exact connector when one identifier has
# multiple connections for the same service. Find these in Scalekit →
# Agent Auth → Connections.
GITHUB_CONNECTOR = os.getenv("GITHUB_CONNECTOR", "github")
GITLAB_CONNECTOR = os.getenv("GITLAB_CONNECTOR", "gitlab")
JIRA_CONNECTOR   = os.getenv("JIRA_CONNECTOR",   "jira")
SLACK_CONNECTOR  = os.getenv("SLACK_CONNECTOR",  "slack")

ENGINEERS_RAW = os.getenv("ENGINEERS", "")

ENGINEER_ID          = os.getenv("ENGINEER_ID", "")
ENGINEER_NAME        = os.getenv("ENGINEER_NAME", "")
GITHUB_USERNAME      = os.getenv("GITHUB_USERNAME", "")
GITHUB_REPOS         = os.getenv("GITHUB_REPOS", "")
GITHUB_ORG           = os.getenv("GITHUB_ORG", "")
GITLAB_PROJECT_PATH  = os.getenv("GITLAB_PROJECT_PATH", "")
GITLAB_USER_ID       = os.getenv("GITLAB_USER_ID", "")
SLACK_USER_ID        = os.getenv("SLACK_USER_ID", "")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL   = os.getenv("OPENROUTER_MODEL",   "anthropic/claude-3-haiku")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


def _trimmed(value: str) -> str:
    return value.strip() if isinstance(value, str) else ""


def validate() -> None:
    """Fail fast on missing required config instead of degrading silently at runtime."""
    missing = []
    if not _trimmed(SCALEKIT_ENV_URL):
        missing.append("SCALEKIT_ENV_URL")
    if not _trimmed(SCALEKIT_CLIENT_ID):
        missing.append("SCALEKIT_CLIENT_ID")
    if not _trimmed(SCALEKIT_CLIENT_SECRET):
        missing.append("SCALEKIT_CLIENT_SECRET")
    if not _trimmed(OPENROUTER_API_KEY):
        missing.append("OPENROUTER_API_KEY")
    if not _trimmed(ENGINEERS_RAW) and not _trimmed(ENGINEER_ID):
        missing.append("ENGINEERS (or ENGINEER_ID for single-engineer mode)")
    if missing:
        raise ValueError(
            f"Missing required env vars: {', '.join(missing)}\n"
            f"Copy .env.example to .env and fill in all values."
        )
