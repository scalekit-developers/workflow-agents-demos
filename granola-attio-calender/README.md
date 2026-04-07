# Sales Call Prep Agent: Granola + Attio + Google Calendar

Polls Google Calendar every 15 minutes for upcoming external meetings. For each one it pulls past meeting notes from Granola, looks up the deal in Attio, synthesizes a 1-page prep brief via LLM, and Slack DMs it to the AE before the call starts.

All OAuth — Google Calendar, Granola MCP, Attio, Slack — is handled by **Scalekit Agent Auth** via `connect.execute_tool()`. No manual token management.

## Connectors

| Connector | Tool names used |
|---|---|
| Google Calendar | `googlecalendar_list_events` |
| Granola MCP | `granolamcp_query_meetings`, `granolamcp_get_meeting_transcript` |
| Attio | `attio_search_records` |
| Slack | `slack_send_message` |

## Setup

**1. Create connectors in Scalekit dashboard:**

Go to your Scalekit workspace and navigate to **Agent Auth > Connections**. Add:
- `googlecalendar` (Google Calendar)
- `granolamcp` (Granola MCP)
- `attio` (Attio)
- `slack` (Slack — with `chat:write` scope)

**2. Configure environment:**

```bash
cp .env.example .env
# Fill in all values — see .env.example for descriptions
```

**3. Install dependencies:**

```bash
pip install -r requirements.txt
```

**4. First run — authorize each connector:**

```bash
python run_flow.py
# Scalekit prints a magic link for any connector not yet authorized.
# Click the link, complete OAuth, press Enter to continue.
```

**5. Run continuously:**

```bash
POLLING_MODE=true python run_flow.py
```

Or as a cron job (every 15 minutes on weekdays, 9am-6pm):

```cron
*/15 9-18 * * 1-5 cd /path/to/granola-attio-calender && python run_flow.py >> logs/run.log 2>&1
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `SCALEKIT_CLIENT_ID` | Yes | From your Scalekit workspace dashboard |
| `SCALEKIT_CLIENT_SECRET` | Yes | From your Scalekit workspace dashboard |
| `SCALEKIT_ENV_URL` | Yes | Your Scalekit environment URL |
| `AE_EMAIL` | Yes | AE's email — used to identify external attendees |
| `SLACK_DM_USER` | Yes | Slack user ID to DM briefs to (starts with `U`) |
| `CALENDAR_USER` | Yes | Scalekit identifier for the Calendar connector |
| `GRANOLA_USER` | Yes | Scalekit identifier for the Granola connector |
| `ATTIO_USER` | Yes | Scalekit identifier for the Attio connector |
| `SLACK_USER` | Yes | Scalekit identifier for the Slack connector |
| `SLACK_CONNECTOR` | Yes | Connector name as set in Scalekit dashboard |
| `GRANOLA_CONNECTOR` | Yes | Connector name as set in Scalekit dashboard |
| `OPENROUTER_API_KEY` | Yes | OpenRouter API key for LLM brief generation |
| `OPENROUTER_MODEL` | No | LLM model (default: `openai/gpt-4o-mini`) |
| `POLLING_MODE` | No | `true` = continuous loop, `false` = one-shot (default) |
| `LOOKAHEAD_MINUTES` | No | Look ahead for meetings starting within N minutes (default: 30) |
| `BRIEF_BEFORE_MINUTES` | No | Only brief if meeting is at least N minutes away (default: 15) |
| `POLL_INTERVAL_MINUTES` | No | How often to poll in polling mode (default: 15) |
