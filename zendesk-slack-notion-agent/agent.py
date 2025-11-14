# Zendesk + Slack + Notion Support Automation Agent
# Main workflow: fetch Zendesk tickets, post Slack digest (with suggested replies), update Notion KB

import os
import json
import requests
import logging
import time
from settings import (
    ZENDESK_API_TOKEN, ZENDESK_EMAIL, ZENDESK_SUBDOMAIN,
    SLACK_BOT_TOKEN, SLACK_CHANNEL_ID,
    NOTION_API_KEY, NOTION_KB_DATABASE_ID, LOG_LEVEL,
)

# Configure logging
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger("zendesk-slack-notion-agent")

# --- Persistent ticket↔thread mapping ---
STATE_FILE = "state/ticket_thread_map.json"
def load_ticket_thread_map():
    """Load the persistent ticket-thread map from disk."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_ticket_thread_map(mapping):
    """Save the persistent ticket-thread map to disk."""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(mapping, f)

# Load persistent mapping at startup
ticket_thread_map = load_ticket_thread_map()

from typing import Optional

# --- Gemini OAuth2 token helper ---
def get_gemini_access_token() -> Optional[str]:
    """
    Fetch a fresh OAuth2 access token for Gemini API using refresh_token credentials from .env.
    Returns the access token string, or None if it fails.
    """
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        logger.error("Missing Google OAuth2 credentials for Gemini in environment.")
        return None
    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    }
    try:
        resp = requests.post(token_url, data=data)
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if not token:
            logger.error(f"No access_token in Gemini OAuth2 response: {resp.text}")
        return token
    except Exception as e:
        logger.error(f"Failed to fetch Gemini access token: {e}")
        return None
class SequentialAgent:
    """
    Orchestrates a sequence of workflow steps, passing context between them.
    Each step is a function that takes and returns a context dict.
    """
    def __init__(self, steps):
        self.steps = steps

    def run(self, context):
        for step in self.steps:
            context = step(context)
        return context


# --- Step functions for the workflow ---
def fetch_new_tickets_step(context):
    """
    Fetch new Zendesk tickets and add to context.
    """
    tickets = get_new_tickets()
    context['tickets'] = tickets
    return context

def generate_suggested_reply_step(context):
    """
    Generate suggested replies for each ticket using Gemini (if configured).
    Adds the reply to each ticket dict.
    """
    for ticket in context.get('tickets', []):
        ticket['suggested_reply'] = generate_suggested_reply_with_gemini(ticket)
    return context

def post_slack_digest_step(context):
    """
    Post a Slack digest for all new tickets not already posted as digest.
    Each ticket block includes subject, ID, status, description, and suggested reply.
    """
    tickets = []
    for ticket in context.get('tickets', []):
        ticket_id = str(ticket["id"])
        if ticket_id in ticket_thread_map:
            logger.info(f"Ticket {ticket_id} already posted to Slack digest; skipping.")
            continue
        tickets.append(ticket)
    if not tickets:
        logger.info("No new tickets for digest; skipping Slack digest.")
        return context
    post_slack_digest(tickets)
    # Mark tickets as posted
    for ticket in tickets:
        ticket_id = str(ticket["id"])
        ticket_thread_map[ticket_id] = "digest"
    save_ticket_thread_map(ticket_thread_map)
    return context

def update_notion_kb_step(context):
    """
    For each solved ticket, update the Notion KB if not already done.
    Uses a separate key in the ticket map to avoid duplicate Notion entries.
    """
    for ticket in context.get('tickets', []):
        ticket_id = str(ticket["id"])
        if ticket.get('status') == 'solved':
            notion_key = f"notion_{ticket_id}"
            if notion_key in ticket_thread_map:
                logger.info(f"Ticket {ticket_id} already updated in Notion; skipping.")
                continue
            update_notion_kb(ticket)
            ticket_thread_map[notion_key] = True
            save_ticket_thread_map(ticket_thread_map)
    return context

# --- End SequentialAgent and steps ---

# Ticket↔Slack thread mapping (persistent)
STATE_FILE = "state/ticket_thread_map.json"

def load_ticket_thread_map():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_ticket_thread_map(mapping):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(mapping, f)

# --- Zendesk API helpers ---
def safe_request(method, url, **kwargs):
    """
    Wrapper for requests to handle Zendesk rate limits and retry logic.
    Retries on 429/401 and logs errors.
    """
    max_retries = 5
    for attempt in range(max_retries):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "60"))
                logger.warning(f"Rate limit hit (429). Sleeping for {retry_after} seconds...")
                time.sleep(retry_after)
                continue
            if resp.status_code == 401:
                logger.warning(f"Unauthorized (401). Possible rate limit or bad credentials. Sleeping 60s...")
                time.sleep(60)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            logger.error(f"Request error: {e}. Attempt {attempt+1}/{max_retries}")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Failed after {max_retries} attempts: {url}")

def get_new_tickets():
    """
    Fetch new Zendesk tickets using the API.
    """
    url = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2/tickets.json?status=new"
    auth = (f"{ZENDESK_EMAIL}/token", ZENDESK_API_TOKEN)
    resp = safe_request("GET", url, auth=auth)
    return resp.json().get("tickets", [])

def get_ticket_details(ticket_id):
    """
    Fetch details for a specific Zendesk ticket.
    """
    url = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2/tickets/{ticket_id}.json"
    auth = (f"{ZENDESK_EMAIL}/token", ZENDESK_API_TOKEN)
    resp = safe_request("GET", url, auth=auth)
    return resp.json().get("ticket", {})

def generate_suggested_reply_with_gemini(ticket):
    """
    Use Google Gemini API (via Google ADK) to generate a suggested reply for the ticket.
    Returns a fallback if Gemini is not configured or fails.
    """
    # Use OAuth2 access token for Gemini API
    access_token = get_gemini_access_token()
    if not access_token:
        return "Thank you for reaching out! We are looking into your issue."
    try:
        # Use Gemini 2.5 Flash Lite model (free/preview)
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        prompt = (
            "You are a helpful support agent. Write only the suggested reply (do not include any explanation, meta-commentary, or bullet points). "
            "The reply should be concise and friendly, ready to send to the customer.\n"
            f"Subject: {ticket['subject']}\nDescription: {ticket['description']}"
        )
        data = {"contents": [{"parts": [{"text": prompt}]}]}
        resp = requests.post(url, headers=headers, json=data)
        resp.raise_for_status()
        result = resp.json()
        reply = result.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
        return reply.strip() or "Thank you for reaching out! We are looking into your issue."
    except Exception as e:
        logger.warning(f"Failed to generate suggested reply with Gemini: {e}")
        return "Thank you for reaching out! We are looking into your issue."

# --- Slack API helpers ---

def post_slack_digest(tickets):
    """
    Post a Slack digest to the channel, using Block Kit for formatting.
    Each ticket includes subject, ID, status, description, and suggested reply.
    """
    if not tickets:
        logger.info("No tickets to include in Slack digest.")
        return
    url = "https://slack.com/api/chat.postMessage"
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json"}
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Daily Support Digest", "emoji": True}},
        {"type": "divider"}
    ]
    for t in tickets:
        ticket_url = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/agent/tickets/{t['id']}"
        subject = t.get('subject', 'No subject')
        status = t.get('status', 'unknown').capitalize()
        desc = t.get('description', '')
        suggested_reply = t.get('suggested_reply', None)
        text = f"*<{ticket_url}|{subject}>*\n*ID:* `{t['id']}`   *Status:* `{status}`\n_{desc}_"
        if suggested_reply:
            text += f"\n>*Suggested reply:* {suggested_reply}"
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": text
            }
        })
        blocks.append({"type": "divider"})
    data = {
        "channel": SLACK_CHANNEL_ID,
        "blocks": blocks,
        "text": "Daily Support Digest"
    }
    resp = requests.post(url, headers=headers, json=data)
    try:
        resp.raise_for_status()
    except Exception:
        logger.error(f"Slack API error: {resp.text}")
        raise
    logger.info(f"Posted Slack digest. Slack response: {resp.text}")

# --- Notion API helpers ---
def update_notion_kb(ticket):
    """
    Update the Notion KB for a solved ticket.
    Only runs if the ticket is marked as solved.
    """
    if ticket.get("status") != "solved":
        logger.info(f"Ticket {ticket['id']} not resolved; skipping Notion KB update.")
        return
    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    # Ensure all required fields are present and valid
    name = f"Ticket {ticket['id']} - {ticket.get('subject', 'No subject')}"
    status = ticket.get("status", "solved")
    description = ticket.get("description", "")
    resolution = ticket.get("resolution") or "Resolved."
    zendesk_id = str(ticket.get("id", ""))
    zendesk_link = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/agent/tickets/{zendesk_id}" if zendesk_id else ""
    # Build Notion properties: Name (title), status (status), Zendesk Link (url)
    properties = {
        "Name": {"title": [{"text": {"content": name}}]},
        "status": {"status": {"name": status}},
        "Zendesk Link": {"url": zendesk_link}
    }
    data = {
        "parent": {"database_id": NOTION_KB_DATABASE_ID},
        "properties": properties,
        "children": [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "text": {"content": description}}
                    ]
                }
            },
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "text": {"content": resolution}}
                    ]
                }
            }
        ]
    }
    logger.info(f"Notion payload: {json.dumps(data, indent=2)}")
    resp = requests.post(url, headers=headers, json=data)
    try:
        resp.raise_for_status()
        logger.info(f"Updated Notion KB for ticket {ticket['id']}")
    except Exception:
        logger.error(f"Notion error response: {resp.text}")
        raise

# ---
# REQUIRED NOTION DATABASE SCHEMA (case/type must match):
# - Name (title)
# - Status (status, must have option 'solved')
# - Zendesk Link (url)

# --- Main loop ---
def main():
    """
    Main loop: runs the SequentialAgent workflow every 60 seconds.
    """
    logger.info("Starting Zendesk + Slack + Notion SequentialAgent workflow...")
    agent = SequentialAgent([
        fetch_new_tickets_step,
        generate_suggested_reply_step,
        post_slack_digest_step,
        update_notion_kb_step
    ])
    while True:
        try:
            agent.run({})
        except Exception as e:
            logger.error(f"Error in SequentialAgent workflow: {e}")
        time.sleep(60)

if __name__ == "__main__":
    main()
