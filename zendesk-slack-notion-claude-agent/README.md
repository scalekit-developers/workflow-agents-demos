# Support Triage Agent: Zendesk + Slack + Notion

An agent that automates support ticket triage by classifying Zendesk tickets with an LLM, searching a Notion knowledge base for matching articles, routing structured alerts to the right Slack channel, and updating the ticket with tags, priority, and an internal note.

All three services are connected through [Scalekit Agent Auth](https://scalekit.com) using a single `actions.execute_tool()` interface. No token management, no OAuth refresh logic, no credential storage in your code.

## What It Does

For every new Zendesk ticket, the agent runs a five-step pipeline:

| Step | Action | Tool |
|------|--------|------|
| 1 | Fetch new/open tickets from Zendesk | `zendesk_search_tickets` |
| 2 | Classify category + severity using an LLM | OpenRouter (GPT-4o-mini) |
| 3 | Search Notion KB for matching articles | `notion_page_search` |
| 4 | Post structured alert to the right Slack channel | `slack_send_message` |
| 5 | Add tags, priority, and internal note to the ticket | `zendesk_ticket_update` + `zendesk_ticket_reply` |

**Classification categories:** billing, bug, feature_request, how_to, account_issue

**Severity levels:** P0 (service down), P1 (major issue), P2 (minor), P3 (question)

## Prerequisites

- Python 3.11+
- A [Scalekit](https://scalekit.com) account (free tier works)
- A Zendesk account with API access enabled
- A Notion workspace with a knowledge base database
- A Slack workspace with routing channels (e.g., #engineering, #billing, #support-triage)
- An [OpenRouter](https://openrouter.ai) API key

## Setup

### 1. Install dependencies

```bash
pip install scalekit-sdk-python requests python-dotenv
```

### 2. Configure environment

```bash
cp .env.example .env
```

Fill in your credentials. See `.env.example` for all available options.

### 3. Set up Scalekit connectors

In the [Scalekit dashboard](https://scalekit.com), add three connectors under Agent Auth > Connections:

**Zendesk** - Enter your Zendesk email and API token. The subdomain must match your Zendesk account (e.g., `yourcompany` for `yourcompany.zendesk.com`).

**Notion** - Complete the OAuth flow, then share your KB database with the integration: open the database in Notion, click `...` > Connections > add your Scalekit integration.

**Slack** - Complete the OAuth flow with `chat:write` scope, then invite the bot to each routing channel: `/invite @your-bot-name`

### 4. Run

```bash
python run_flow.py
```

## How It Works

```
-- Step 0: Checking connector auth --
  zendesk (support@yourcompany.com) -- ACTIVE
  slack (support@yourcompany.com) -- ACTIVE
  notion (support@yourcompany.com) -- ACTIVE

-- Step 1: Fetching new Zendesk tickets --
  Found 3 new ticket(s)

-- Step 2: Classifying ticket --
  Ticket #4: "Billing charged twice for March subscription"
  Category: billing | Severity: P1

-- Step 3: Searching Notion KB --
  Skipped (category 'billing' does not require KB search)

-- Step 4: Routing to Slack --
  Posted to #billing

-- Step 5: Updating Zendesk ticket --
  Tags added, internal note created

Flow complete. Processed 3 ticket(s).
```

## Configuration

All configuration is done through environment variables in `.env`:

### Required

| Variable | Description |
|----------|-------------|
| `SCALEKIT_ENV_URL` | Your Scalekit workspace URL |
| `SCALEKIT_CLIENT_ID` | Scalekit client ID |
| `SCALEKIT_CLIENT_SECRET` | Scalekit client secret |
| `ZENDESK_USER` | Email used for the Zendesk connected account |
| `SLACK_USER` | Email used for the Slack connected account |
| `NOTION_USER` | Email used for the Notion connected account |
| `OPENROUTER_API_KEY` | OpenRouter API key for LLM classification |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `SLACK_CONNECTOR` | `slack` | Connector name in Scalekit dashboard |
| `NOTION_DB_ID` | _(empty)_ | Target a specific Notion database for KB search |
| `OPENROUTER_MODEL` | `openai/gpt-4o-mini` | LLM model for classification |
| `POLLING_MODE` | `false` | Set to `true` for continuous polling |
| `POLL_INTERVAL_MINUTES` | `2` | Minutes between polling cycles |
| `CHANNEL_BUG` | `#engineering` | Slack channel for bug tickets |
| `CHANNEL_BILLING` | `#billing` | Slack channel for billing tickets |
| `CHANNEL_FEATURE` | `#product-feedback` | Slack channel for feature requests |
| `CHANNEL_HOWTO` | `#support-triage` | Slack channel for how-to questions |
| `CHANNEL_ACCOUNT` | `#support-triage` | Slack channel for account issues |
| `FALLBACK_CHANNEL` | `#support-triage` | Fallback channel if primary post fails |

## Polling Mode

For continuous monitoring, enable polling:

```env
POLLING_MODE=true
POLL_INTERVAL_MINUTES=2
```

For business-hours-only coverage via cron:

```bash
*/2 9-18 * * 1-5 cd /path/to/agent && python run_flow.py >> logs/run.log 2>&1
```

## Architecture

```
Zendesk  ──┐
            ├──  Scalekit Agent Auth  ──  run_flow.py  ──  OpenRouter LLM
Notion   ──┤     (execute_tool)
            │
Slack    ──┘
```

Every API call to Zendesk, Notion, and Slack goes through Scalekit's `actions.execute_tool()`. Token refresh, scope management, and connection state are handled automatically.

## Project Structure

```
zendesk-slack-notion-claude-agent/
  run_flow.py       # The complete triage agent (single file)
  .env.example      # Template for environment variables
  .gitignore        # Ignores .env, state/, __pycache__/
  README.md         # This file
```
