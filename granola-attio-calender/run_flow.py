"""
Sales Call Prep Agent: Google Calendar → Granola → Attio → Slack

Polls Google Calendar for upcoming external meetings. For each one it:
  1. Pulls past meeting notes and transcripts from Granola
  2. Looks up the deal and stakeholders in Attio
  3. Synthesizes a 1-page prep brief via LLM (OpenRouter)
  4. Slack DMs the brief to the AE before the call starts

All OAuth — Google Calendar, Granola, Attio, Slack — is handled by
Scalekit Agent Auth via connect.execute_tool(). No manual token management.

Run once:
  python run_flow.py

Run as a continuous loop:
  POLLING_MODE=true python run_flow.py
"""
import os, time, sys
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import scalekit.client

load_dotenv()

# ── Scalekit client ───────────────────────────────────────────────────────────
sk = scalekit.client.ScalekitClient(
    client_id=os.environ["SCALEKIT_CLIENT_ID"],
    client_secret=os.environ["SCALEKIT_CLIENT_SECRET"],
    env_url=os.environ["SCALEKIT_ENV_URL"],
)
connect = sk.connect

SLACK_CONNECTOR   = os.environ["SLACK_CONNECTOR"]
GRANOLA_CONNECTOR = os.environ.get("GRANOLA_CONNECTOR", "granolamcp")
CONNECTOR_USERS = {
    "googlecalendar":  os.environ["CALENDAR_USER"],
    GRANOLA_CONNECTOR: os.environ["GRANOLA_USER"],
    "attio":           os.environ["ATTIO_USER"],
    SLACK_CONNECTOR:   os.environ["SLACK_USER"],
}

AE_EMAIL         = os.environ["AE_EMAIL"]
SLACK_DM_USER    = os.environ["SLACK_DM_USER"]
LOOKAHEAD_MIN    = int(os.environ.get("LOOKAHEAD_MINUTES", "30"))
BRIEF_BEFORE_MIN = int(os.environ.get("BRIEF_BEFORE_MINUTES", "15"))
POLL_INTERVAL_MIN = int(os.environ.get("POLL_INTERVAL_MINUTES", "15"))
POLLING_MODE     = os.environ.get("POLLING_MODE", "false").lower() == "true"
OPENROUTER_KEY   = os.environ["OPENROUTER_API_KEY"]
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")

_sent_event_ids: set[str] = set()
_unavailable: set[str] = set()


# ── Auth helpers ──────────────────────────────────────────────────────────────
def ensure_authorized(connector: str) -> None:
    from scalekit.common.exceptions import ScalekitNotFoundException
    identifier = CONNECTOR_USERS.get(connector, "")
    try:
        resp = connect.get_or_create_connected_account(
            connection_name=connector, identifier=identifier
        )
        status = resp.connected_account.status
        if status != "ACTIVE":
            link = connect.get_authorization_link(
                connection_name=connector, identifier=identifier
            ).link
            print(f"\n  [{connector}] Not authorized. Open to connect:\n    {link}\n")
            try:
                input("  Press Enter after authorizing...")
            except EOFError:
                _unavailable.add(connector)
                return
            resp2 = connect.get_or_create_connected_account(
                connection_name=connector, identifier=identifier
            )
            if resp2.connected_account.status == "ACTIVE":
                print(f"  ✓ {connector} — ACTIVE")
            else:
                print(f"  ✗ {connector} — still not ACTIVE")
                _unavailable.add(connector)
        else:
            print(f"  ✓ {connector} ({identifier}) — ACTIVE")
    except ScalekitNotFoundException:
        print(f"  ✗ {connector} — not found in Scalekit (skipping)")
        _unavailable.add(connector)
    except Exception as e:
        print(f"  ✗ {connector} — {e.__class__.__name__}: {e}")
        _unavailable.add(connector)


def tool(connector: str, tool_name: str, **kwargs) -> dict:
    if connector in _unavailable:
        return {}
    result = connect.execute_tool(
        tool_name=tool_name,
        identifier=CONNECTOR_USERS[connector],
        tool_input=kwargs,
    )
    return result.data or {}


# ── Step 1: Calendar ──────────────────────────────────────────────────────────
def get_upcoming_external_meetings() -> list[dict]:
    now = datetime.now(timezone.utc)
    window = now + timedelta(minutes=LOOKAHEAD_MIN)
    ae_domain = AE_EMAIL.split("@")[-1].lower()

    events_data = tool(
        "googlecalendar", "googlecalendar_list_events",
        calendar_id="primary",
        time_min=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        time_max=window.strftime("%Y-%m-%dT%H:%M:%SZ"),
        single_events=True,
        order_by="startTime",
        max_results=20,
    )

    meetings = []
    for ev in (events_data.get("events") or events_data.get("items") or []):
        attendees = ev.get("attendees") or []
        external = [
            a for a in attendees
            if "@" in (a.get("email") or "")
            and not (a.get("email") or "").lower().endswith(f"@{ae_domain}")
            and not (a.get("email") or "").lower().endswith(".calendar.google.com")
        ]
        if not external:
            continue

        start_str = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date", "")
        if not start_str:
            continue
        start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))

        minutes_until = (start_dt - now).total_seconds() / 60
        if minutes_until < BRIEF_BEFORE_MIN:
            continue

        meetings.append({
            "id":              ev.get("id", ""),
            "title":           ev.get("summary") or "Untitled Meeting",
            "start_dt":        start_dt,
            "minutes_until":   minutes_until,
            "external_emails": [a["email"] for a in external],
        })

    return meetings


# ── Step 2: Granola ───────────────────────────────────────────────────────────
def get_granola_history(attendee_emails: list[str], company_name: str) -> tuple[list[dict], list[str]]:
    notes, transcripts = [], []
    seen_ids: set[str] = set()

    for query in ([*attendee_emails, company_name] if company_name else attendee_emails)[:3]:
        try:
            data = tool(GRANOLA_CONNECTOR, "granolamcp_query_meetings", query=query, limit=5)
        except Exception:
            continue
        for m in (data.get("meetings") or data.get("results") or []):
            m_id = m.get("id") or m.get("meetingId") or ""
            if m_id in seen_ids:
                continue
            seen_ids.add(m_id)
            notes.append(m)

    notes.sort(key=lambda m: m.get("date") or m.get("startTime") or "", reverse=True)
    notes = notes[:5]

    for note in notes[:2]:
        m_id = note.get("id") or note.get("meetingId") or ""
        tx = ""
        if m_id:
            try:
                data = tool(GRANOLA_CONNECTOR, "granolamcp_get_meeting_transcript", meeting_id=m_id)
                raw = data.get("transcript") or data.get("notes") or data.get("summary") or ""
                tx = "\n".join(str(t.get("text") or t.get("content") or t) for t in raw) \
                    if isinstance(raw, list) else str(raw)
            except Exception:
                pass
        transcripts.append(tx)

    while len(transcripts) < len(notes):
        transcripts.append("")

    return notes, transcripts


# ── Step 3: Attio ─────────────────────────────────────────────────────────────
def get_attio_deal(attendee_emails: list[str]) -> dict:
    # Search deals by company domain or person email
    queries = set()
    for email in attendee_emails:
        queries.add(email)
        domain_part = email.split("@")[-1].split(".")[0]  # e.g. "techvista" from techvista.io
        queries.add(domain_part)

    for query in list(queries)[:4]:
        deal_data = tool("attio", "attio_search_records", object="deals", query=query, limit=3)
        deals = deal_data.get("data") or deal_data.get("records") or []
        if not deals:
            continue

        d = deals[0]
        vals = d.get("values", {})

        def first(key: str) -> str:
            v = vals.get(key, [])
            if isinstance(v, list) and v:
                item = v[0]
                return (
                    item.get("value")
                    or item.get("text")
                    or item.get("status", {}).get("title") if isinstance(item.get("status"), dict) else item.get("status")
                    or ""
                )
            return str(v) if v else ""

        return {
            "id":    d.get("id", {}).get("record_id") or d.get("id") or "",
            "name":  first("name"),
            "stage": first("stage"),
            "value": first("value") or "0",
            "last_activity": first("last_activity_date") or first("last_contact_date"),
        }

    return {}


# ── Step 4: Brief synthesis ───────────────────────────────────────────────────
def synthesize_brief(
    meeting_title: str,
    attendees: list[str],
    granola_notes: list[dict],
    deal: dict,
    transcripts: list[str],
) -> str:
    notes_text = ""
    for i, (note, tx) in enumerate(zip(granola_notes, transcripts), 1):
        date  = note.get("date") or note.get("startTime") or "Unknown date"
        title = note.get("title") or note.get("name") or f"Meeting {i}"
        notes_text += f"\nMeeting {i}: {title} ({date})\n"
        if tx:
            notes_text += tx[:1500] + ("..." if len(tx) > 1500 else "") + "\n"
        elif note.get("summary") or note.get("notes"):
            notes_text += str(note.get("summary") or note.get("notes") or "")[:800] + "\n"

    deal_text = (
        f"Stage: {deal.get('stage', 'Unknown')} | "
        f"Value: ${deal.get('value', '0')} | "
        f"Last activity: {deal.get('last_activity', 'Unknown')}"
        if deal else "No deal found in Attio — likely a new prospect."
    )

    prompt = f"""You are a sales intelligence assistant. Create a concise prep brief for an upcoming sales call.

Meeting: {meeting_title}
Attendees (external): {', '.join(attendees)}
Deal info: {deal_text}

Past meeting notes:
{notes_text or 'No prior meetings found — this is a first call.'}

Generate a structured brief with exactly these sections:
1. **Prior Context** — What was discussed in prior calls? Key decisions, open items, relationship tone.
2. **Deal Status** — Stage, value, close date, urgency level.
3. **Key Stakeholders** — Who is in the meeting and their likely priorities.
4. **Suggested Agenda** — 3-4 specific talking points based on open action items and deal stage.
5. **Open Questions** — 2-3 targeted questions to probe.

Rules:
- Be specific, not generic. Reference actual details from past notes.
- If there are no prior meetings, say so and suggest discovery questions.
- Keep each section to 2-4 bullet points.
- Total brief should be scannable in 2 minutes.
- Return plain text with the section headers as shown above."""

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
        json={
            "model": OPENROUTER_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        },
        timeout=45,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


# ── Step 5: Slack DM ──────────────────────────────────────────────────────────
def send_slack_brief(event: dict, brief: str, deal: dict, granola_notes: list[dict]) -> None:
    now = datetime.now(timezone.utc)
    mins = int((event["start_dt"] - now).total_seconds() / 60)

    deal_line = (
        f"\n📊 *Deal:* {deal.get('name', 'Unknown')} | "
        f"Stage: {deal.get('stage', 'N/A')} | "
        f"Value: ${deal.get('value', '0')}"
        if deal else ""
    )
    granola_line = (
        f"\n📝 *Prior meetings:* {len(granola_notes)} found in Granola"
        if granola_notes else ""
    )

    message = (
        f"🗓 *Call Prep Brief — {event['title']}*\n"
        f"Starting in *{mins} minutes* · {event['start_dt'].strftime('%I:%M %p %Z')}"
        f"{deal_line}"
        f"\n👥 *Attendees:* {', '.join(event['external_emails'])}"
        f"{granola_line}"
        f"\n\n{brief}"
        f"\n\n_Powered by Google Calendar + Granola + Attio + Scalekit Agent Auth_"
    )

    result = connect.execute_tool(
        tool_name="slack_send_message",
        identifier=CONNECTOR_USERS[SLACK_CONNECTOR],
        tool_input={"channel": SLACK_DM_USER, "text": message},
    )
    ts = (result.data or {}).get("timestamp", "")
    print(f"  ✓ Brief sent for '{event['title']}' (ts={ts})")


# ── Main loop ─────────────────────────────────────────────────────────────────
def process_cycle() -> None:
    print(f"\n── Poll cycle: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ──")

    print("\n── Step 1: Checking upcoming calendar events ──")
    meetings = get_upcoming_external_meetings()
    print(f"  Found {len(meetings)} external meeting(s) in next {LOOKAHEAD_MIN} min")
    if not meetings:
        return

    for event in meetings:
        if event["id"] in _sent_event_ids:
            print(f"  ↳ Already sent brief for '{event['title']}' — skipping")
            continue

        print(f"\n  Meeting: {event['title']}")
        print(f"  Starts in: {int(event['minutes_until'])} min")
        print(f"  External attendees: {event['external_emails']}")

        company_name = event["external_emails"][0].split("@")[-1].split(".")[0].capitalize() \
            if event["external_emails"] else ""

        print("\n── Step 2: Pulling Granola meeting history ──")
        granola_notes, transcripts = get_granola_history(event["external_emails"], company_name)
        print(f"  Found {len(granola_notes)} prior meeting(s) in Granola")

        print("\n── Step 3: Looking up Attio deal ──")
        deal = get_attio_deal(event["external_emails"])
        if deal:
            print(f"  Deal: {deal.get('name')} | Stage: {deal.get('stage')} | Value: ${deal.get('value')}")
        else:
            print("  No deal found in Attio")

        print("\n── Step 4: Synthesizing prep brief ──")
        brief = synthesize_brief(
            meeting_title=event["title"],
            attendees=event["external_emails"],
            granola_notes=granola_notes,
            deal=deal,
            transcripts=transcripts,
        )
        print("  ✓ Brief ready")

        print("\n── Step 5: Sending Slack brief ──")
        send_slack_brief(event, brief, deal, granola_notes)
        _sent_event_ids.add(event["id"])

    print("\n✓ Flow complete.")


# ── Entry point ───────────────────────────────────────────────────────────────
print("\n── Step 0: Checking connector auth ──")
for connector in ("googlecalendar", GRANOLA_CONNECTOR, "attio", SLACK_CONNECTOR):
    ensure_authorized(connector)

if "googlecalendar" in _unavailable:
    print("\n✗ Google Calendar unavailable — cannot continue.")
    sys.exit(1)

if POLLING_MODE:
    print(f"\n✓ Polling every {POLL_INTERVAL_MIN} min. Ctrl+C to stop.\n")
    while True:
        process_cycle()
        print(f"\n  Sleeping {POLL_INTERVAL_MIN} min…")
        time.sleep(POLL_INTERVAL_MIN * 60)
else:
    process_cycle()
