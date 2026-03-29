# Post-Meeting Action Agent

Automates the post-call admin loop: reads your Granola meeting notes, updates the HubSpot deal, drafts a Gmail follow-up, and posts a Slack summary — in under 30 seconds.

Built with [Scalekit Agent Auth](https://scalekit.com) for OAuth across all four services.

```
Call ends → Granola: fetch notes
         → HubSpot: update deal
         → Gmail:   create draft
         → Slack:   post summary
```

## Prerequisites

- [Scalekit account](https://scalekit.com) — free tier works
- Granola Business plan (MCP API requires it)
- HubSpot, Gmail, and Slack accounts
- Python 3.11+

## Setup

### 1. Set up Scalekit connectors

Go to **app.scalekit.com → Agent Auth → Connections** and create four connectors:

| Connection Name | Service |
|---|---|
| `granolamcp` | Granola MCP |
| `hubspot` | HubSpot CRM |
| `gmail` | Gmail |
| `slack` (or custom name) | Slack |

Copy your API credentials from **Settings → API Credentials**.

### 2. Configure environment

```bash
cp .env.example .env
```

Fill in `.env`:

```bash
SCALEKIT_ENV_URL=https://your-env.scalekit.dev
SCALEKIT_CLIENT_ID=skc_xxxxxxxxxxxx
SCALEKIT_CLIENT_SECRET=your_secret

GRANOLA_USER=you@yourcompany.com
HUBSPOT_USER=you@yourcompany.com
GMAIL_USER=you@yourcompany.com
SLACK_USER=you@yourcompany.com

SLACK_CONNECTOR=slack          # connection name from Scalekit dashboard
SLACK_CHANNEL=C0XXXXXXXXX     # channel ID from Slack URL

# Optional: smarter LLM extraction (falls back to rule-based parser if not set)
OPENROUTER_API_KEY=
OPENROUTER_MODEL=meta-llama/llama-3.1-8b-instruct:free
```

> **Finding your Slack channel ID:** open the channel in Slack, copy the ID from the URL (`/archives/C0XXXXXXXXX`), or leave `SLACK_CHANNEL` blank and run the agent once to list channels.

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run

```bash
python run_flow.py
```

The first run checks auth for each connector. If any are not yet authorized, a magic link is printed — open it, complete OAuth, press Enter. Every subsequent run goes straight through.

## How it works

```
Step 0 — Auth check
  Scalekit verifies all four connectors are ACTIVE.
  Prints a magic link for any that need authorization.

Step 1 — Granola
  Fetches recent meetings via granolamcp_list_meetings.
  Pulls transcripts via granolamcp_get_meeting_transcript.
  Falls back to granolamcp_query_granola_meetings for manual notes.

Step 2 — HubSpot
  Searches for an existing deal by company name.
  Creates a new deal if none found.
  Updates the deal with meeting summary and action items.

Step 3 — Gmail
  Gets a fresh OAuth token from Scalekit.
  Creates a personalized follow-up draft via the Gmail API.
  Draft goes to Drafts folder — rep reviews before sending.

Step 4 — Slack
  Posts a summary (title, next step, action items, deal link)
  to the configured channel via slack_send_message.
```

## LLM extraction

If `OPENROUTER_API_KEY` is set, the agent uses an LLM to extract structured data from the meeting (company, deal stage, amount, action items, email body).

If the key is not set — or the LLM call fails — it falls back to a built-in rule-based parser automatically. No manual configuration required.

## Project structure

```
├── run_flow.py          # main pipeline
├── connectors/
│   └── gmail.py         # Gmail draft creation (token + REST API)
├── .env.example         # environment template
├── requirements.txt
└── README.md
```

## Triggering automatically

**Webhook (recommended):** Granola's `meeting.completed` event fires 2–5 minutes after a call ends. Point it at a lightweight Flask or FastAPI endpoint that calls `run_flow.py`.

**Cron (simpler):** Run every 5 minutes. Track processed meeting IDs in a local JSON file to avoid duplicates.

## Notes

- **Granola:** Business plan required for transcript access and the `meeting.completed` webhook.
- **HubSpot:** Token refresh (30-minute expiry) is handled automatically by Scalekit.
- **Gmail:** No `gmail_create_draft` Scalekit tool exists yet — the agent uses `get_connected_account()` to get a token and calls the Gmail REST API directly. Once Scalekit ships the tool, it becomes a one-line swap.
- **Slack:** Re-authorize if you see `token_expired` errors — run the agent and open the printed magic link.
