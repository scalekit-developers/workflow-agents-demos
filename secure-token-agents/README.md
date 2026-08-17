# HubSpot OAuth: Naïve vs LangChain vs ScaleKit

This folder demonstrates three ways to handle HubSpot OAuth tokens and show an intentional expiry/refresh flow.

## Files
- `naive_agent.py` — Direct HubSpot OAuth usage. Reads an access token from `.env`, tries a call, and if it fails (401/403) it refreshes using refresh_token and retries.
- `hubspot_langchain_flow.py` — A LangChain Runnable pipeline that orchestrates: initial fetch → wait N seconds → simulate expiry → fetch again (triggers refresh). Uses the functions in the naïve script under the hood.
- `scalekit_hubspot_flow.py` — Same flow but with tokens managed by a ScaleKit OAuth endpoint (SCALEKIT_TOKEN_URL). On 401/403, refreshes via ScaleKit and retries, optionally persisting rotated tokens to `.env`.
- `.env` — Place HubSpot and (optionally) ScaleKit credentials here.
- `requirements.txt` — Dependencies.

## Setup
```bash
cd secure-token-agents 
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Populate `.env`:

Required for naïve + LangChain flows:
- HUBSPOT_CLIENT_ID=
- HUBSPOT_CLIENT_SECRET=
- HUBSPOT_REFRESH_TOKEN=
- HUBSPOT_ACCESS_TOKEN=   # optional; if present we try it first
- REDIRECT_URI=http://localhost:3000

Optional knobs:
- SAVE_TOKENS_TO_ENV=1            # persist refreshed tokens back to .env
- SIMULATE_EXPIRE=1               # only used by naive script for instant simulation
- SIMULATE_EXPIRE_DELAY=60        # used by flows to wait before simulating expiry

For ScaleKit flow (replace with your tenant details):
- SCALEKIT_TOKEN_URL=https://hey.scalekit.dev
- SCALEKIT_CLIENT_ID=
- SCALEKIT_CLIENT_SECRET=
- SCALEKIT_REFRESH_TOKEN=

## Run

Naïve one-shot call (will refresh on 401/403 and retry):
```bash
python naive_agent.py
```

LangChain flow demo (initial fetch → wait → simulate → refresh → fetch):
```bash
# quick demo
SIMULATE_EXPIRE_DELAY=3 python hubspot_langchain_flow.py
```

ScaleKit-managed flow (ScaleKit-only; no fallback):
```bash
# quick demo (requires ScaleKit env vars below)
SIMULATE_EXPIRE_DELAY=3 python scalekit_hubspot_flow.py
```

## What this shows
- Naïve: Your app holds refresh token and client secret; you call HubSpot directly.
- LangChain: Orchestrates the same logic in a clear, composable pipeline.
- ScaleKit: Externalizes OAuth token management and rotation; your app calls ScaleKit to refresh and then calls HubSpot.

If your ScaleKit config rotates refresh tokens, the demo persists them back to `.env` when `SAVE_TOKENS_TO_ENV=1`.

## ScaleKit-only setup (no fallback)
Set these in `secure-token-agents/.env` for the ScaleKit flow:
- SCALEKIT_TOKEN_URL: Your ScaleKit OAuth token endpoint (example: https://hey.scalekit.dev)
- SCALEKIT_CLIENT_ID: Client ID from your ScaleKit app
- SCALEKIT_CLIENT_SECRET: Client Secret from your ScaleKit app
- SCALEKIT_REFRESH_TOKEN: Refresh token issued by ScaleKit that maps to your HubSpot connection

Where to get them in ScaleKit:
- Sign in to app.scalekit and create/configure an application for HubSpot access.
- Retrieve Client ID and Client Secret from your application settings.
- Connect/authorize HubSpot for your account/tenant inside ScaleKit to obtain a refresh token.
- Copy the OAuth token endpoint URL for your environment (Prod/Dev) as SCALEKIT_TOKEN_URL.

