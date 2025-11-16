import os
from dotenv import load_dotenv

load_dotenv()

ZENDESK_API_TOKEN = os.getenv("ZENDESK_API_TOKEN")
ZENDESK_EMAIL = os.getenv("ZENDESK_EMAIL")
ZENDESK_SUBDOMAIN = os.getenv("ZENDESK_SUBDOMAIN")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID")
NOTION_API_KEY = os.getenv("NOTION_API_KEY")
NOTION_KB_DATABASE_ID = os.getenv("NOTION_KB_DATABASE_ID")

# Prefer GOOGLE_API_KEY; fall back to GOOGLE_ADK_API_KEY for backward compatibility
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_ADK_API_KEY")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

REQUIRED_VARS = [
    ("ZENDESK_API_TOKEN", ZENDESK_API_TOKEN),
    ("ZENDESK_EMAIL", ZENDESK_EMAIL),
    ("ZENDESK_SUBDOMAIN", ZENDESK_SUBDOMAIN),
    ("SLACK_BOT_TOKEN", SLACK_BOT_TOKEN),
    ("SLACK_CHANNEL_ID", SLACK_CHANNEL_ID),
    ("NOTION_API_KEY", NOTION_API_KEY),
    ("NOTION_KB_DATABASE_ID", NOTION_KB_DATABASE_ID),
    ("GOOGLE_API_KEY (or GOOGLE_ADK_API_KEY)", GOOGLE_API_KEY),
]

missing = [name for name, val in REQUIRED_VARS if not val]
if missing:
    raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")
