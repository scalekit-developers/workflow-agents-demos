"""
Post-Meeting Action Agent: Granola → HubSpot → Gmail → Slack

Scalekit handles OAuth for all four connectors — no manual token management.
LLM extraction (OpenRouter) is used when OPENROUTER_API_KEY is set.
Falls back to a rule-based parser automatically if the key is missing or the call fails.

Setup:
  cp .env.example .env        # fill in your credentials
  pip install -r requirements.txt
  python run_flow.py
"""
import os, json, re
from dotenv import load_dotenv
import scalekit.client
from connectors.gmail import create_draft

load_dotenv()

# ── Scalekit client ──────────────────────────────────────────────────────────
sk = scalekit.client.ScalekitClient(
    client_id=os.environ["SCALEKIT_CLIENT_ID"],
    client_secret=os.environ["SCALEKIT_CLIENT_SECRET"],
    env_url=os.environ["SCALEKIT_ENV_URL"],
)
connect = sk.connect

SLACK_CONNECTOR = os.environ.get("SLACK_CONNECTOR", "slack")
CONNECTOR_USERS = {
    "granolamcp":    os.environ["GRANOLA_USER"],
    "hubspot":       os.environ["HUBSPOT_USER"],
    "gmail":         os.environ["GMAIL_USER"],
    SLACK_CONNECTOR: os.environ["SLACK_USER"],
}
SLACK_CHANNEL = os.environ["SLACK_CHANNEL"]


# ── Auth helpers ─────────────────────────────────────────────────────────────
def ensure_authorized(connector: str) -> None:
    """Check connector status. Prints a magic link if not yet authorized."""
    identifier = CONNECTOR_USERS[connector]
    resp = connect.get_or_create_connected_account(
        connection_name=connector, identifier=identifier
    )
    if resp.connected_account.status != "ACTIVE":
        link = connect.get_authorization_link(
            connection_name=connector, identifier=identifier
        ).link
        print(f"\n[{connector}] Not authorized. Open:\n  {link}\n")
        input("Press Enter after authorizing...")
    else:
        print(f"  ✓ {connector} ({identifier}) — ACTIVE")


def get_token(connector: str) -> str:
    """Fetch a fresh OAuth token. Always call immediately before an API call."""
    identifier = CONNECTOR_USERS[connector]
    return connect.get_connected_account(
        connection_name=connector, identifier=identifier
    ).connected_account.authorization_details["oauth_token"]["access_token"]


# ── Extraction ───────────────────────────────────────────────────────────────
def _extract_with_llm(text: str) -> dict:
    """Call OpenRouter to extract structured deal info from meeting text."""
    import requests as http

    prompt = f"""Extract structured information from this meeting note. Return ONLY valid JSON with these exact keys:
- company (string)
- contact_email (string or null)
- deal_name (string, e.g. "Acme — Q2 Deal")
- deal_stage (one of: appointmentscheduled, qualifiedtobuy, presentationscheduled, decisionmakerboughtin, contractsent, closedwon, closedlost)
- amount (integer USD or null)
- summary (2-3 sentence summary)
- action_items (list of strings)
- next_step (string)
- email_subject (string)
- email_body (string, use \\n for newlines)

Meeting note:
{text}"""

    resp = http.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
        json={
            "model": os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        },
        timeout=30,
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"].strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    start, end = raw.find("{"), raw.rfind("}") + 1
    return json.loads(raw[start:end])


def _extract_rule_based(text: str, title: str = "") -> dict:
    """Fallback parser — extracts key fields from meeting text without an LLM."""
    tl = text.lower()

    # Amount
    m = re.search(r'\$\s*([\d,]+)', text)
    amount = int(m.group(1).replace(",", "")) if m else None

    # Deal stage
    stage = "appointmentscheduled"
    if "contract" in tl:
        stage = "contractsent"
    elif "closed won" in tl:
        stage = "closedwon"
    elif "proposal" in tl or "presentation" in tl:
        stage = "presentationscheduled"
    elif "qualified" in tl:
        stage = "qualifiedtobuy"

    # Company — look for capitalized noun near deal/account keywords, else use title
    cm = re.search(
        r'([A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+)*)\s+(?:deal|account|company|team|corp|inc)',
        text,
    )
    company = cm.group(1) if cm else (title.split("—")[0].strip() or "Unknown")

    # Contact email
    em = re.search(r'[\w.+-]+@[\w-]+\.[a-z]{2,}', text)
    contact_email = em.group(0) if em else None

    # Action items — lines starting with a bullet or dash
    action_items = [a.strip() for a in re.findall(r'[-•*]\s+(.+)', text) if len(a.strip()) > 5]

    # Summary — first two sentences
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    summary = " ".join(sentences[:2]) if sentences else text[:200]

    next_step = action_items[0] if action_items else "Follow up with the contact"

    email_body = (
        "Hi,\n\n"
        "Thanks for the meeting. Here's a quick recap:\n\n"
        f"{summary}\n\n"
        "Action items:\n" +
        "\n".join(f"• {a}" for a in action_items) +
        f"\n\nNext step: {next_step}\n\nBest regards"
    )

    return {
        "company":       company,
        "contact_email": contact_email,
        "deal_name":     f"{company} — Deal",
        "deal_stage":    stage,
        "amount":        amount,
        "summary":       summary,
        "action_items":  action_items,
        "next_step":     next_step,
        "email_subject": f"Follow-up: {company} — next steps",
        "email_body":    email_body,
    }


def extract_meeting_info(text: str, title: str = "") -> dict:
    """LLM extraction if OPENROUTER_API_KEY is set; rule-based parser otherwise."""
    if os.environ.get("OPENROUTER_API_KEY"):
        try:
            result = _extract_with_llm(text)
            print("  (LLM extraction ✓)")
            return result
        except Exception as e:
            print(f"  ⚠ LLM failed ({e.__class__.__name__}: {e}) — using rule-based parser")
    return _extract_rule_based(text, title)


# ── Step 0: Check auth ────────────────────────────────────────────────────────
print("\n── Step 0: Checking connector auth ──")
for connector in ("granolamcp", "hubspot", "gmail", SLACK_CONNECTOR):
    ensure_authorized(connector)


# ── Step 1: Fetch meetings from Granola ──────────────────────────────────────
print("\n── Step 1: Fetching meetings from Granola ──")
granola_user = CONNECTOR_USERS["granolamcp"]

meetings_result = connect.execute_tool(
    tool_name="granolamcp_list_meetings",
    identifier=granola_user,
    tool_input={"limit": 3},
)
meetings_text = "".join(
    c.get("text", "") for c in (meetings_result.data or {}).get("content", [])
    if isinstance(c, dict)
)
meeting_ids    = re.findall(r'id="([^"]+)"', meetings_text)
meeting_titles = re.findall(r'title="([^"]+)"', meetings_text)

if not meeting_ids:
    print("No meetings found in Granola. Record a call and try again.")
    exit(0)

print(f"Found {len(meeting_ids)} meeting(s): {meeting_titles}")

meeting_data = []
for mid, mtitle in zip(meeting_ids, meeting_titles):
    print(f"\n  Fetching: {mtitle} ({mid[:8]}…)")

    # Try transcript first
    tx_result = connect.execute_tool(
        tool_name="granolamcp_get_meeting_transcript",
        identifier=granola_user,
        tool_input={"meeting_id": mid},
    )
    tx_raw = "".join(
        c.get("text", "") for c in (tx_result.data or {}).get("content", [])
        if isinstance(c, dict)
    ).strip()

    try:
        content = json.loads(tx_raw).get("transcript") or ""
    except (json.JSONDecodeError, AttributeError):
        content = tx_raw

    # Fall back to note query if transcript is empty
    if len(content) < 30:
        print("  Transcript empty — querying note content…")
        q_result = connect.execute_tool(
            tool_name="granolamcp_query_granola_meetings",
            identifier=granola_user,
            tool_input={"query": mtitle},
        )
        content = "".join(
            c.get("text", "") for c in (q_result.data or {}).get("content", [])
            if isinstance(c, dict)
        ).strip()

    if len(content) < 30:
        print("  No content found — skipping.")
        continue

    print(f"  Content: {len(content)} chars")
    meeting_data.append({"id": mid, "title": mtitle, "transcript": content})


# ── Step 2: Extract info and sync to HubSpot ─────────────────────────────────
print("\n── Step 2: Extracting info & syncing to HubSpot ──")
hs_user = CONNECTOR_USERS["hubspot"]
processed = []

for m in meeting_data:
    print(f"\n  Processing: {m['title']}")
    info = extract_meeting_info(m["transcript"], m["title"])
    print(f"  → Company: {info['company']}  Stage: {info['deal_stage']}  Amount: ${info['amount'] or 'N/A'}")

    # Find or create deal
    search = connect.execute_tool(
        tool_name="hubspot_deals_search",
        identifier=hs_user,
        tool_input={"query": info["company"], "limit": 3},
    )
    deals = (search.data or {}).get("results", [])

    if deals:
        deal_id   = deals[0]["id"]
        deal_name = deals[0]["properties"].get("dealname", deal_id)
        print(f"  Found deal: {deal_name} (id={deal_id})")
    else:
        created = connect.execute_tool(
            tool_name="hubspot_deal_create",
            identifier=hs_user,
            tool_input={
                "dealname":  info["deal_name"],
                "dealstage": info["deal_stage"],
                "amount":    info["amount"] or 0,
            },
        )
        deal_id   = (created.data or {}).get("id", "unknown")
        deal_name = info["deal_name"]
        print(f"  Created deal: {deal_name} (id={deal_id})")

    action_str = "\n".join(f"• {a}" for a in info["action_items"])
    connect.execute_tool(
        tool_name="hubspot_deal_update",
        identifier=hs_user,
        tool_input={
            "deal_id": deal_id,
            "properties": {
                "description": f"Meeting summary:\n{info['summary']}\n\nAction items:\n{action_str}",
                "dealstage":   info["deal_stage"],
            },
        },
    )
    print(f"  Updated deal {deal_id} ✓")
    processed.append({**m, **info, "deal_id": deal_id, "deal_name": deal_name})


# ── Step 3: Create Gmail drafts ───────────────────────────────────────────────
print("\n── Step 3: Creating Gmail drafts ──")
gm_token = get_token("gmail")

for p in processed:
    to      = p.get("contact_email") or CONNECTOR_USERS["gmail"]
    subject = p["email_subject"]
    body    = p["email_body"]
    draft   = create_draft(gm_token, to=to, subject=subject, body=body)
    print(f"  Draft → {to}  |  {subject}  |  id: {draft.get('id')} ✓")


# ── Step 4: Post summaries to Slack ──────────────────────────────────────────
print("\n── Step 4: Posting summaries to Slack ──")
slack_user = CONNECTOR_USERS[SLACK_CONNECTOR]

for p in processed:
    action_items = "\n".join(f"• {a}" for a in p.get("action_items", []))
    message = (
        f"📞 *{p['title']}*\n\n"
        f"{p['summary']}\n\n"
        f"*Next Step:* {p['next_step']}\n\n"
        f"*Action Items:*\n{action_items or '• None'}\n\n"
        f"*Deal:* {p['deal_name']} (id={p['deal_id']})"
    )
    result = connect.execute_tool(
        tool_name="slack_send_message",
        identifier=slack_user,
        tool_input={"channel": SLACK_CHANNEL, "text": message},
    )
    ts = (result.data or {}).get("timestamp", "")
    print(f"  Posted to Slack ✓ (ts={ts})")

print("\n✓ Flow complete.\n")
