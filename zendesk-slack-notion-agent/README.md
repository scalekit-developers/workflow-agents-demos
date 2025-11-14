
# Zendesk + Slack + Notion Support Automation Agent

Automate customer support workflows: when a new Zendesk ticket arrives, post a Slack summary and Gemini-suggested reply for human approval; upon ticket resolution, update the Notion knowledge base automatically.

## What it does
- Polls Zendesk for new tickets (API poll)
- Posts a summary and Gemini-suggested reply to a mapped Slack thread for human-in-the-loop approval
- On ticket resolution, updates the Notion KB with the resolution/answer
- Supports ticket↔thread mapping, rate limits, and basic retries

## Quick Start

1. Install dependencies
    ```bash
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    ```

2. Configure environment
    ```bash
    cp .env.example .env
    # Edit .env with your Zendesk, Slack, Notion, and Google ADK API keys and settings
    ```

3. Start the agent (polls Zendesk for ticket updates every 60 seconds)
    ```bash
    python agent.py
    ```

## Files
- `agent.py` – Main automation logic: ticket polling, Slack/Notion/Gemini integration, mapping
- `settings.py` – Environment variable loading and validation
- `requirements.txt` – Python dependencies
- `.env.example` – Example environment config
- `.gitignore` – Ignore .env, .venv, __pycache__, and state files

## Env vars (.env)
- ZENDESK_API_TOKEN, ZENDESK_EMAIL, ZENDESK_SUBDOMAIN
- SLACK_BOT_TOKEN, SLACK_CHANNEL_ID
- NOTION_API_KEY, NOTION_KB_DATABASE_ID
- GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN (for Gemini API)
- LOG_LEVEL (default: INFO)

## Notes
- Human-in-the-loop: Slack thread approval before sending reply
- Ticket↔thread mapping for context
- Handles API rate limits and retries
- All third-party calls use scoped API keys
