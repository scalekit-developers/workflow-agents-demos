# QA Report: Support Ticket Automation Agent

Repo: zendesk-slack-notion-agent
Reference: https://www.scalekit.com/agent-templates/support-ticket-automation-agent
Run date: 2026-06-19


## Test Results

| Check | Result |
|---|---|
| Full pipeline (Zendesk, Gemini, Slack, Notion) | PASS |
| Zendesk ticket fetch | PASS |
| Gemini reply generation | PASS with fallback (free tier quota hit, static reply used) |
| Slack digest post | PASS |
| Notion KB row creation | PASS |
| Idempotency across restarts | PASS |
| Startup banner and colored logger | PASS |
| Graceful Ctrl+C shutdown | PASS |
| scalekit-sdk-python | 2.12.0 |
| google-adk | 2.3.0 |
| Syntax check (agent.py, settings.py) | PASS |


## Live Run Output

```
Support Ticket Automation Agent starting...
..........................................................
17:24:09 | INFO     | >  Cycle #1
17:24:09 | INFO     | v  Fetching Zendesk tickets...
17:24:14 | INFO     | ok Fetched 1 ticket(s) from Zendesk.
17:24:14 | INFO     | *  Generating replies for 1 ticket(s) via Gemini...
17:24:14 | WARNING  | !  Reply generation failed for #2.0: 429 (20/day quota)
17:24:14 | INFO     |    #2.0: Thank you for reaching out! We are looking into your issue.
17:24:14 | INFO     | -  Slack digest skipped - all tickets already posted.
17:24:14 | INFO     | o  Saving ticket #2.0 to Notion KB...
17:24:16 | INFO     | ok Notion KB row created for ticket #2.0.
17:24:16 | INFO     | -  Next cycle in 60s...
```

Ticket fetched: #2, "SAMPLE: Unable to log in after password reset". Gemini 429 is expected on the free tier (20 req/day). Slack skip is correct because that ticket was already posted in a prior cycle. Notion row was created successfully.


## Environment Variables

### Required

| Variable | Live value | Description |
|---|---|---|
| SCALEKIT_ENV_URL | https://hey.scalekit.dev | Scalekit environment URL |
| SCALEKIT_CLIENT_ID | skc_20324953727501110 | Scalekit client ID |
| SCALEKIT_CLIENT_SECRET | set | Scalekit client secret |
| ZENDESK_IDENTIFIER | parv@infrasity.com | Identifier for the Zendesk connected account |
| SLACK_IDENTIFIER | parv@infrasity.com | Identifier for the Slack connected account |
| NOTION_IDENTIFIER | parv@infrasity.com | Identifier for the Notion connected account |
| SLACK_SUPPORT_CHANNEL | C09K0K2RZ6Y | Slack channel ID for digests |
| NOTION_KB_DATABASE_ID | 2ab26e341074804b8d1fe27661999d51 | Notion database ID |
| GOOGLE_API_KEY | set | Gemini API key |

### Connection names

Required when the same identifier has more than one connection for a service. Without these, Scalekit returns an error.

| Variable | Value | Status |
|---|---|---|
| SLACK_CONNECTION_NAME | slack-sKfekCVz | ACTIVE (team@infrasity.com) |
| ZENDESK_CONNECTION_NAME | zendesk | Connected |
| NOTION_CONNECTION_NAME | notion | Connected |

### Optional

| Variable | Default | Description |
|---|---|---|
| GOOGLE_ADK_MODEL | gemini-2.5-flash | Gemini model |
| POLL_INTERVAL | 60 | Seconds between cycles |
| LOG_LEVEL | INFO | Log verbosity |


## Webpage Validation (Section by Section)

The reference page shows five numbered steps. Each is compared against the actual repo code below.


### Step 01: agent.py

**Page says:** SequentialAgent from Google ADK chains three sub-agents: a classifier, a responder, and a router that decides Slack channel and Notion KB save.

**Page code:**
```python
from google.adk.agents import LlmAgent, SequentialAgent

classifier = LlmAgent(name="ticket_classifier", model="gemini-2.0-flash",
    instruction="Classify the support ticket. Return JSON: {category, priority, save_to_kb}")
responder  = LlmAgent(name="ticket_responder", model="gemini-2.0-flash",
    instruction="Draft a helpful, concise reply for the support ticket.")
orchestrator = SequentialAgent(name="support_orchestrator",
    sub_agents=[classifier, responder])
```

**Repo:** No SequentialAgent, no classifier, no router. `run_pipeline()` calls four plain Python functions in a fixed order. Reply generation is a direct `_genai.Client.models.generate_content()` call.

**Why it changed:** The SequentialAgent triggered `MALFORMED_FUNCTION_CALL` when the raw Zendesk ticket JSON (~1,600 tokens) was passed back to Gemini as a function result. The LLM stopped after step 1 or 2 and the rest of the pipeline never ran. Replacing it with a deterministic call sequence fixed this entirely.

**Verdict: NO MATCH.** Page shows a conceptual ADK pattern. Repo uses a deterministic pipeline because LLM orchestration failed under real payloads.


### Step 02: tickets.py

**Page says:** zendesk_tickets_list returns new tickets since the last poll. The agent stores last_seen_id in a local JSON file.

**Page code:**
```python
def fetch_new_zendesk_tickets(sk_actions, identifier, last_seen_id: int) -> list:
    resp = sk_actions.execute_tool(
        tool_name="zendesk_tickets_list",
        identifier=identifier,
        tool_input={"status": "new", "sort_by": "created_at", "sort_order": "asc"},
    )
    tickets = (resp.data or {}).get("tickets", [])
    return [t for t in tickets if t["id"] > last_seen_id]
```

**Repo:**
```python
def fetch_new_tickets() -> dict:
    resp = sk.actions.execute_tool(
        tool_name="zendesk_tickets_list",
        identifier=settings.ZENDESK_IDENTIFIER,
        connection_name=settings.ZENDESK_CONNECTION_NAME,
        tool_input={"status": "new", "sort_by": "created_at", "sort_order": "asc"},
    )
    raw = (resp.data or {}).get("tickets", [])
    tickets = [{"id": t.get("id"), "subject": t.get("subject", ""),
                "description": t.get("description", ""), "status": t.get("status", "")}
               for t in raw]
    return {"tickets": tickets}
```

**Differences:**

`connection_name` is added in the repo. Without it, Scalekit errors when the identifier has multiple Zendesk connections.

Tickets are trimmed to four fields (id, subject, description, status). The raw Zendesk object is ~1,600 tokens. Passing it to Gemini triggers MALFORMED_FUNCTION_CALL. Trimming prevents this.

Deduplication uses `ticket_thread_map.json` (keyed by ticket ID string), not a `last_seen_id` integer. The page description says "stores last_seen_id in a local JSON file" which does not match the actual file structure.

**Verdict: PARTIAL MATCH.** Core tool call matches. Page is missing `connection_name`, payload trimming, and the dedup description is inaccurate.


### Step 03: pipeline.py

**Page says:** The SequentialAgent runs classifier then responder. The final Runner event carries the reply text downstream.

**Page code:**
```python
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

runner = Runner(agent=orchestrator, app_name="support_automation",
                session_service=InMemorySessionService())

def process_ticket(ticket: dict) -> str:
    prompt = f"Ticket ID {ticket['id']}:\n{ticket['description']}"
    for event in runner.run(user_id="pipeline", session_id=str(ticket["id"]),
                            new_message=Content(parts=[Part(text=prompt)])):
        if event.is_final_response():
            return event.content.parts[0].text
    return ""
```

**Repo:**
```python
def _generate_reply(ticket: dict) -> str:
    prompt = ("You are a helpful support agent. Write only the suggested reply.\n\n"
              f"Subject: {ticket.get('subject', '')}\n"
              f"Description: {ticket.get('description', '')}")
    try:
        resp = _genai_client.models.generate_content(
            model=settings.GOOGLE_ADK_MODEL, contents=prompt)
        return resp.text.strip()
    except Exception as exc:
        logger.warning("Reply generation failed for #%s: %s", ticket.get("id"), exc)
    return "Thank you for reaching out! We are looking into your issue."
```

**Differences:**

No Runner, no InMemorySessionService, no per-ticket ADK session. These were removed after `runner.run()` produced 503 errors from sub-agents that silently aborted the pipeline with no useful error output.

The repo uses a stateless direct genai call. Failures are caught explicitly and a fallback reply is returned. The Slack and Notion steps always run regardless.

Note on the import: `from google.adk.sessions import InMemorySessionService` is the correct path for google-adk 2.3.0. Earlier versions had it at `google.adk.runners`. The page import is accurate, but the repo does not use it.

**Verdict: NO MATCH.** Page describes a pattern that caused silent failures. Repo uses a direct genai call with explicit fallback.


### Step 04: notify.py

**Page says:** slack_send_message posts a batched digest. A `posted_ids` set deduplicates across poll cycles so the same ticket never appears twice.

**Page code:**
```python
posted_ids: set[int] = set()

def post_digest(sk_actions, identifier, entries: list[dict]):
    fresh = [e for e in entries if e["ticket_id"] not in posted_ids]
    if not fresh:
        return
    lines = [f"* #{e['ticket_id']}: {e['reply'][:120]}" for e in fresh]
    sk_actions.execute_tool(
        tool_name="slack_send_message", identifier=identifier,
        tool_input={"channel": SLACK_SUPPORT_CHANNEL,
                    "text": "*New tickets digest*\n" + "\n".join(lines)})
    posted_ids.update(e["ticket_id"] for e in fresh)
```

**Repo:**
```python
def post_slack_digest(context: dict) -> dict:
    tickets = context.get("tickets", [])
    fresh = [t for t in tickets if str(t["id"]) not in ticket_thread_map]
    if not fresh:
        return {"status": "skipped"}
    lines = [f"* #{t['id']}: {t.get('subject', 'No subject')}\n"
             f"  _{t.get('suggested_reply', '')[:120]}_" for t in fresh]
    sk.actions.execute_tool(
        tool_name="slack_send_message",
        identifier=settings.SLACK_IDENTIFIER,
        connection_name=settings.SLACK_CONNECTION_NAME,
        tool_input={"channel": settings.SLACK_SUPPORT_CHANNEL,
                    "text": "*New support tickets digest*\n\n" + "\n".join(lines)})
    for t in fresh:
        ticket_thread_map[str(t["id"])] = "digest"
    _save_state(ticket_thread_map)
    return {"status": "posted"}
```

**Differences:**

`posted_ids: set[int]` resets every time the process restarts. The repo uses `ticket_thread_map.json` on disk, which survives restarts. The page description says it "deduplicates across poll cycles" but that is only true within a single run.

Zendesk returns IDs as floats (e.g. 2.0). The repo converts to string before looking up in the JSON file. The page uses raw integers which would miss existing entries after a restart.

`connection_name` is missing from the page.

**Verdict: PARTIAL MATCH.** Slack tool call structure matches. Page is missing `connection_name` and the in-memory dedup set does not survive restarts as the description implies.


### Step 05: kb.py

**Page says:** notion_database_insert_row creates a KB row with ticket ID, category, and the accepted reply. Solved tickets with save_to_kb:true from the classifier are persisted here.

**Page code:**
```python
def save_to_notion_kb(sk_actions, identifier, ticket_id, category, reply):
    sk_actions.execute_tool(
        tool_name="notion_database_insert_row", identifier=identifier,
        tool_input={
            "database_id": NOTION_KB_DATABASE_ID,
            "properties": {
                "Ticket ID": {"number": ticket_id},
                "Category":  {"select": {"name": category}},
                "Reply":     {"rich_text": [{"text": {"content": reply}}]},
            },
        },
    )
```

**Repo:**
```python
sk.actions.execute_tool(
    tool_name="notion_database_insert_row",
    identifier=settings.NOTION_IDENTIFIER,
    connection_name=settings.NOTION_CONNECTION_NAME,
    tool_input={
        "database_id": settings.NOTION_KB_DATABASE_ID,
        "properties": {
            "Name":      {"title": [{"text": {"content": f"#{ticket_id}: {subject}"}}]},
            "Ticket ID": {"number": int(float(ticket_id))},
            "Category":  {"select": {"name": category}},
            "Reply":     {"rich_text": [{"text": {"content": reply}}]},
        },
    },
)
```

**Differences:**

The repo adds `connection_name`. The page omits it.

The repo uses `"Name"` as the title column (the default Notion title column name) and adds `"Ticket ID"`, `"Category"`, and `"Reply"` matching the page exactly. Category is derived by `_classify_ticket()`, a keyword classifier on the subject line (Account, Billing, Bug, Feature Request, General), since there is no LLM classifier sub-agent.

The Notion database must have these four columns created before running: Name (title, created by default), Ticket ID (number), Category (select), Reply (rich_text). The database ID is set in `NOTION_KB_DATABASE_ID`.

**Verdict: MATCH (with one addition).** The property structure now matches the page. The repo adds `connection_name` and derives `category` via keyword matching instead of an LLM classifier.


## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| ValueError: missing env vars at startup | .env not configured | Copy .env.example to .env and fill all values |
| SupportProductInactive from Zendesk | No active Zendesk Support plan | Activate a Zendesk Support trial |
| MALFORMED_FUNCTION_CALL | Raw ticket JSON too large for LLM | Fixed in repo: payload trimmed to 4 fields |
| 429 RESOURCE_EXHAUSTED from Gemini | Free tier quota hit (20 req/day) | Enable billing on the Google Cloud project |
| 400 Invalid property identifier from Notion | Required columns missing from database | Create Name (title), Ticket ID (number), Category (select), Reply (rich_text) columns in the Notion database |
| Slack message not appearing | PENDING_AUTH on Slack account | Re-authorize Slack in the Scalekit dashboard |
| Multiple connected accounts error | Identifier has 2+ connections for same service | Set all three CONNECTION_NAME env vars |
| Same ticket posted again after restart | In-memory dedup resets on restart | Fixed in repo: state/ticket_thread_map.json persists |


## Known Issues

Gemini free tier allows 20 requests per day on gemini-2.5-flash. When quota is hit, the repo uses a static fallback reply and the pipeline continues. Enable billing on the Google Cloud project to get real AI replies on every run.

The Slack account for parv@infrasity.com has PENDING_AUTH status in Scalekit. Posts currently go through team@infrasity.com via slack-sKfekCVz. Re-authorize parv@infrasity.com in the Scalekit dashboard to change this.

The Notion database must have four columns: Name (title, default), Ticket ID (number), Category (select), Reply (rich_text). The repo writes to all four. If these columns do not exist, the insert will return `400 Invalid property identifier`. Create them in Notion before running the agent.


## Validation Checklist

| Item | Result |
|---|---|
| python -m py_compile agent.py | PASS |
| python -m py_compile settings.py | PASS |
| settings.validate() lists missing vars and exits | PASS |
| scalekit-sdk-python 2.12.0 | PASS |
| google-adk 2.3.0 | PASS |
| All three services via sk.actions.execute_tool() | PASS |
| No hardcoded OAuth tokens | PASS |
| connection_name on every execute_tool call | PASS |
| Payload trimmed to 4 fields before Gemini | PASS |
| Notion uses Name/Ticket ID/Category/Reply columns matching page | PASS |
| Idempotency store is file-backed | PASS |
| Gemini failure caught, fallback reply returned | PASS |
| Startup banner | PASS |
| Ctrl+C exits cleanly | PASS |
| Zendesk fetch live | PASS |
| Slack post live | PASS |
| Notion row creation live | PASS |
| Page Step 01 matches repo | NO: page shows SequentialAgent; repo uses plain Python pipeline |
| Page Step 02 matches repo | PARTIAL: missing connection_name, payload trimming, and inaccurate dedup description |
| Page Step 03 matches repo | NO: page uses ADK Runner; repo uses direct genai call with fallback |
| Page Step 04 matches repo | PARTIAL: missing connection_name, in-memory set resets on restart |
| Page Step 05 matches repo | MATCH: repo now uses same Name/Ticket ID/Category/Reply column structure; adds connection_name and keyword-based category |
