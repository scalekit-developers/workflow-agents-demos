import os
from dotenv import load_dotenv

load_dotenv()

SCALEKIT_ENV_URL       = os.getenv("SCALEKIT_ENV_URL", "")
SCALEKIT_CLIENT_ID     = os.getenv("SCALEKIT_CLIENT_ID", "")
SCALEKIT_CLIENT_SECRET = os.getenv("SCALEKIT_CLIENT_SECRET", "")

APOLLO_USER  = os.getenv("APOLLO_USER", "")
GMAIL_USER   = os.getenv("GMAIL_USER", "")
SHEETS_USER  = os.getenv("SHEETS_USER", "")

# Connector names — pin to exact connector when one identifier has multiple
# connections for the same service. Find these in Scalekit → Connected Accounts.
APOLLO_CONNECTION_NAME  = os.getenv("APOLLO_CONNECTION_NAME", "apollo")
GMAIL_CONNECTION_NAME   = os.getenv("GMAIL_CONNECTION_NAME", "gmail")
SHEETS_CONNECTION_NAME  = os.getenv("SHEETS_CONNECTION_NAME", "googlesheets")

SHEETS_ID    = os.getenv("SHEETS_ID", "")
SHEETS_RANGE = os.getenv("SHEETS_RANGE", "Sheet1!A:H")

ICP_TITLES     = [t.strip() for t in os.getenv("ICP_TITLES",     "VP of Sales,Head of Sales,Director of Sales,CRO").split(",")]
ICP_INDUSTRIES = [i.strip() for i in os.getenv("ICP_INDUSTRIES", "SaaS,Software,Technology").split(",")]
ICP_EMP_MIN    = int(os.getenv("ICP_EMP_MIN",    "50"))
ICP_EMP_MAX    = int(os.getenv("ICP_EMP_MAX",    "5000"))
PROSPECT_LIMIT = int(os.getenv("PROSPECT_LIMIT", "5"))

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL   = os.getenv("OPENROUTER_MODEL",   "google/gemma-3-27b-it:free")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


def validate() -> None:
    missing = []
    if not SCALEKIT_ENV_URL:
        missing.append("SCALEKIT_ENV_URL")
    if not SCALEKIT_CLIENT_ID:
        missing.append("SCALEKIT_CLIENT_ID")
    if not SCALEKIT_CLIENT_SECRET:
        missing.append("SCALEKIT_CLIENT_SECRET")
    if not APOLLO_USER:
        missing.append("APOLLO_USER")
    if not GMAIL_USER:
        missing.append("GMAIL_USER")
    if not SHEETS_USER:
        missing.append("SHEETS_USER")
    if missing:
        raise ValueError(
            f"Missing required env vars: {', '.join(missing)}\n"
            f"Copy .env.example to .env and fill in all values."
        )
