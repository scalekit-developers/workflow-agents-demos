"""
Gmail connector — creates email drafts via the Gmail REST API v1.
Token is fetched from Scalekit (connector: gmail).
"""
import base64
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


def _build_raw_message(to: str, subject: str, body: str, reply_to: str | None = None) -> str:
    msg = MIMEMultipart("alternative")
    msg["to"] = to
    msg["subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.attach(MIMEText(body, "plain"))
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def create_draft(access_token: str, to: str, subject: str, body: str) -> dict:
    """Create a Gmail draft (not sent — stays in Drafts folder).

    Returns the created draft object including its id.
    """
    raw = _build_raw_message(to, subject, body)
    response = requests.post(
        f"{GMAIL_BASE}/drafts",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"message": {"raw": raw}},
    )
    response.raise_for_status()
    return response.json()


def list_drafts(access_token: str, max_results: int = 5) -> list[dict]:
    """List existing drafts (id + snippet)."""
    response = requests.get(
        f"{GMAIL_BASE}/drafts",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"maxResults": max_results},
    )
    response.raise_for_status()
    return response.json().get("drafts", [])
