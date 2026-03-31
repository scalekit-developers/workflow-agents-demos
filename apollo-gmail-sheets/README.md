# Outbound Prospecting Agent

Searches Apollo for ICP-matched contacts, drafts personalized Gmail outreach with an LLM, and logs everything to a Google Sheets tracker — so SDRs spend time selling, not on admin.

**Built with [Scalekit Agent Auth](https://scalekit.com).** All OAuth across Apollo, Gmail, and Google Sheets is handled by Scalekit — no token management, no refresh logic.

## What it does

```
1. Search   → Apollo: find contacts by title, industry, company size
2. Enrich   → Apollo: add buying signals and org context per contact
3. Score    → rank 0–100 against your ICP; top results advance
4. Draft    → LLM writes a personalized email per prospect
5. Gmail    → saves each email as a DRAFT (you review before sending)
6. Sheets   → logs name, company, ICP score, signals, and draft link
```

## Quick start

### 1. Set up Scalekit connectors

Go to [app.scalekit.com](https://app.scalekit.com) → **Agent Auth → Connections** and add:

| Connection name | Service       |
|-----------------|---------------|
| `apollo`        | Apollo        |
| `gmail`         | Gmail         |
| `googlesheets`  | Google Sheets |

Copy your API credentials from **Settings → API Credentials**.

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your credentials. Key values:

| Variable | Where to find it |
|---|---|
| `SCALEKIT_ENV_URL` | Scalekit dashboard — workspace URL |
| `SCALEKIT_CLIENT_ID` | Settings → API Credentials |
| `SCALEKIT_CLIENT_SECRET` | Settings → API Credentials |
| `GMAIL_USER` | your Gmail address |
| `SHEETS_USER` | your Google account email |
| `SHEETS_ID` | the long ID in your Sheet URL |

> **Finding your Sheet ID:** open your Google Sheet and copy the alphanumeric string from `docs.google.com/spreadsheets/d/**SHEET_ID**/edit`

### 3. Install and run

```bash
pip install -r requirements.txt
python run_flow.py
```

The first run checks auth for each connector. If any aren't authorized yet, a magic link is printed — open it, complete OAuth, press Enter. Every run after that goes straight through.

**No Apollo account yet?**

```bash
USE_SAMPLE_DATA=true python run_flow.py
```

Skips Apollo entirely and uses five built-in sample prospects. Gmail drafts and Sheets logging still run against your real accounts.

## How it works

```
Step 0 — Auth check
  Scalekit verifies all connectors are ACTIVE.
  Prints a magic link for any that need authorization.

Step 1 — Find prospects
  Calls apollo_search_contacts with your ICP filters (title, industry, headcount).
  Calls apollo_enrich_contact for each result to add buying signals and org context.
  Scores each prospect 0–100 against ICP criteria.
  Takes the top PROSPECT_LIMIT results.

Step 2 — Draft emails
  LLM (OpenRouter) writes a personalized email anchored to real buying signals.
  Falls back to a signal-aware template if no API key is configured.

Step 3 — Gmail
  Retrieves a fresh OAuth token from Scalekit's token vault.
  Creates each email as a Gmail DRAFT via the Gmail REST API.
  Never sends — the SDR reviews and sends when ready.

Step 4 — Google Sheets
  Appends one row per prospect: name, company, title, email, ICP score,
  buying signals, email subject, and a direct link to the Gmail draft.
```

## ICP scoring

| Factor | Points | Criteria |
|--------|--------|----------|
| Title match | 30 | Matches `ICP_TITLES` list |
| Industry match | 25 | Matches `ICP_INDUSTRIES` list |
| Company size | 20 | Within `ICP_EMP_MIN`–`ICP_EMP_MAX` range |
| Buying signals | up to 25 | 5 pts per signal, capped at 25 |

All ICP criteria live in `.env` — no code changes needed to retarget.

## Project structure

```
├── run_flow.py       main pipeline
├── .env.example      environment template
├── requirements.txt
└── README.md
```

## Notes

- **Apollo:** enrichment credits are consumed per lookup — start with `PROSPECT_LIMIT=5` and increase gradually
- **Gmail:** `gmail.compose` scope required — for internal Google Workspace use, no additional Google verification is needed
- **Google Sheets:** the connector name in Scalekit must be `googlesheets` (not `google-sheets`)
- **Scalekit token vault:** tokens for all three connectors are stored and auto-refreshed by Scalekit — no expiry handling needed in application code
- **Sample mode:** `USE_SAMPLE_DATA=true` runs the full pipeline without an Apollo connector
