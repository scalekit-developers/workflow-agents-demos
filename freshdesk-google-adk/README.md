# Freshdesk → Google ADK Customer Feedback Automation Agent

## Overview
This agent automates customer follow-up for Freshdesk tickets using Google ADK. It:
- Polls Freshdesk for tickets marked as resolved
- Fetches the latest CSAT survey result for each ticket
- Uses Google ADK (SequentialAgent) to decide and execute actions (reply, close, or reopen) based on the survey result
- Posts a public reply to the Freshdesk ticket and updates its status
- Deduplicates by ticket ID (no double follow-ups)
- Provides robust logging and error handling

## Quickstart

### 1. Clone & Install
```bash
cd freshdesk-google-adk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Environment
```bash
cp .env.example .env
# Edit .env with your Freshdesk and Google ADK credentials
```

### 3. Run the Agent
```bash
python agent.py
```

## Environment Variables

See `.env.example` for all required variables. You must set:
- `FRESHDESK_DOMAIN` – Your Freshdesk domain (e.g., `yourcompany.freshdesk.com`)
- `FRESHDESK_API_KEY` – Your Freshdesk API key
- `GOOGLE_ADK_API_KEY` – Your Google ADK API key
- `POLL_INTERVAL` – (Optional) Polling interval in seconds (default: 60)

## Files
- `agent.py` – Main polling and automation logic (well-commented)
- `.env.example` – Example environment config
- `requirements.txt` – Python dependencies
- `processed_tickets.json` – Local state for deduplication (auto-created)
- `.gitignore` – Excludes sensitive and unnecessary files

## Troubleshooting
- Ensure your `.env` is correct and not checked into git
- The agent requires the `google-adk` Python SDK in the active virtual environment
- If you see API errors, check your credentials and network access
- For debugging, review the console logs (DEBUG/ERROR/INFO)

## Contributing
Open issues or PRs for improvements, bugfixes, or documentation updates.

## License
MIT
