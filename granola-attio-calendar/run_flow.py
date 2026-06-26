"""
Sales Call Prep Agent: Google Calendar + Granola + Attio + Slack

Polls Google Calendar for upcoming external meetings. For each one it:
  1. Pulls past meeting notes and transcripts from Granola
  2. Looks up the deal and stakeholders in Attio
  3. Synthesizes a 1-page prep brief via Claude (Anthropic)
  4. Slack DMs the brief to the AE before the call starts

All OAuth — Google Calendar, Granola, Attio, Slack — is handled by
Scalekit Agent Auth via actions.execute_tool(). No manual token management.

Run once:
  python run_flow.py

Run as a continuous loop:
  POLLING_MODE=true python run_flow.py
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging — same style as gmail-gcalender-scheduling-agent
# ---------------------------------------------------------------------------

ICONS = {
    "auth":   "🔑",
    "cal":    "📅",
    "notes":  "📝",
    "deal":   "💼",
    "brief":  "✦",
    "slack":  "💬",
    "done":   "✔",
    "skip":   "–",
    "warn":   "⚠",
    "error":  "✖",
    "poll":   "⌕",
}

CYAN  = "\033[36m"
BOLD  = "\033[1m"
WHITE = "\033[97m"
GREY  = "\033[90m"
RESET = "\033[0m"


def _setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    fmt = "%(asctime)s | %(levelname)-8s | %(message)s"
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format=fmt,
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    for noisy in ("httpx", "httpcore", "urllib3", "grpc"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_setup_logging()
log = logging.getLogger("granola-prep-agent")

# ---------------------------------------------------------------------------
# Config — fail fast if required vars are missing
# ---------------------------------------------------------------------------

_REQUIRED = [
    "SCALEKIT_CLIENT_ID",
    "SCALEKIT_CLIENT_SECRET",
    "SCALEKIT_ENV_URL",
    "AE_EMAIL",
    "SLACK_DM_USER",
    "CALENDAR_USER",
    "GRANOLA_USER",
    "ATTIO_USER",
    "SLACK_USER",
    "SLACK_CONNECTOR",
    "ANTHROPIC_API_KEY",
]
_missing = [k for k in _REQUIRED if not os.getenv(k)]
if _missing:
    for k in _missing:
        log.error("%s  Missing required env var: %s", ICONS["error"], k)
    sys.exit(1)

SLACK_CONNECTOR   = os.environ["SLACK_CONNECTOR"]
GRANOLA_CONNECTOR = os.environ.get("GRANOLA_CONNECTOR", "granolamcp")

CONNECTOR_USERS = {
    "googlecalendar":  os.environ["CALENDAR_USER"],
    GRANOLA_CONNECTOR: os.environ["GRANOLA_USER"],
    "attio":           os.environ["ATTIO_USER"],
    SLACK_CONNECTOR:   os.environ["SLACK_USER"],
}

AE_EMAIL          = os.environ["AE_EMAIL"]
SLACK_DM_USER     = os.environ["SLACK_DM_USER"]
LOOKAHEAD_MIN     = int(os.getenv("LOOKAHEAD_MINUTES", "10080"))
BRIEF_BEFORE_MIN  = int(os.getenv("BRIEF_BEFORE_MINUTES", "15"))
POLL_INTERVAL_MIN = int(os.getenv("POLL_INTERVAL_MINUTES", "15"))
POLLING_MODE      = os.getenv("POLLING_MODE", "false").lower() == "true"
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL      = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# ---------------------------------------------------------------------------
# Scalekit client
# ---------------------------------------------------------------------------

from scalekit import ScalekitClient

_sk_client: Optional[ScalekitClient] = None


def _get_client() -> ScalekitClient:
    global _sk_client
    if _sk_client is None:
        _sk_client = ScalekitClient(
            client_id=os.environ["SCALEKIT_CLIENT_ID"],
            client_secret=os.environ["SCALEKIT_CLIENT_SECRET"],
            env_url=os.environ["SCALEKIT_ENV_URL"],
        )
    return _sk_client


# Track connectors that failed auth so we skip them gracefully
_unavailable: set[str] = set()
_sent_event_ids: set[str] = set()

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------


def _banner() -> None:
    tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    if not tty:
        print("Sales Call Prep Agent starting...")
        return
    ae_masked = ("****" + AE_EMAIL[-8:]) if len(AE_EMAIL) > 8 else "****"
    line = f"{CYAN}{BOLD}{'─' * 60}{RESET}"
    print(line)
    print(f"{CYAN}{BOLD}  Sales Call Prep Agent{RESET}")
    print(f"{GREY}  Calendar + Granola + Attio → Claude brief → Slack DM{RESET}")
    print(line)
    print(f"  {GREY}AE email     :{RESET} {WHITE}{ae_masked}{RESET}")
    print(f"  {GREY}Lookahead    :{RESET} {WHITE}{LOOKAHEAD_MIN} min{RESET}")
    print(f"  {GREY}Brief before :{RESET} {WHITE}{BRIEF_BEFORE_MIN} min{RESET}")
    print(f"  {GREY}Poll interval:{RESET} {WHITE}{POLL_INTERVAL_MIN} min{RESET}")
    print(f"  {GREY}LLM model    :{RESET} {WHITE}{CLAUDE_MODEL}{RESET}")
    print(line)
    print()

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def ensure_authorized(connector: str) -> None:
    """Ensure a Scalekit connector is ACTIVE; walk user through OAuth if not."""
    identifier = CONNECTOR_USERS.get(connector, "")
    client = _get_client()
    try:
        resp = client.actions.get_or_create_connected_account(
            connection_name=connector,
            identifier=identifier,
        )
        account = resp.connected_account

        if account.status != "ACTIVE":
            link_resp = client.actions.get_authorization_link(
                connection_name=connector,
                identifier=identifier,
            )
            log.warning("%s  %s not authorized — open this URL to connect:",
                        ICONS["auth"], connector)
            log.warning("    %s", link_resp.link)
            try:
                input("\n    Press Enter after completing authorization in your browser...\n")
            except EOFError:
                log.warning("%s  Non-interactive mode — skipping %s", ICONS["warn"], connector)
                _unavailable.add(connector)
                return

            resp2 = client.actions.get_connected_account(
                connection_name=connector,
                identifier=identifier,
            )
            if resp2.connected_account.status == "ACTIVE":
                log.info("%s  %s connected for %s", ICONS["done"], connector, identifier)
            else:
                log.error("%s  %s still not ACTIVE after authorization — skipping",
                          ICONS["error"], connector)
                _unavailable.add(connector)
        else:
            log.info("%s  %s connected for %s", ICONS["done"], connector, identifier)

    except Exception as exc:
        log.error("%s  %s auth failed (%s: %s) — skipping",
                  ICONS["error"], connector, type(exc).__name__, exc)
        _unavailable.add(connector)


def _tool(connector: str, tool_name: str, **kwargs) -> dict:
    """Call a Scalekit connector tool; returns {} on any failure."""
    if connector in _unavailable:
        log.debug("%s  Skipping %s — connector %s unavailable",
                  ICONS["skip"], tool_name, connector)
        return {}
    try:
        client = _get_client()
        resp = client.actions.execute_tool(
            tool_name=tool_name,
            identifier=CONNECTOR_USERS[connector],
            tool_input=kwargs,
        )
        return resp.data if hasattr(resp, "data") and resp.data else {}
    except Exception as exc:
        log.debug("%s  Tool %s failed: %s", ICONS["warn"], tool_name, exc)
        return {}

# ---------------------------------------------------------------------------
# Step 1: Google Calendar — upcoming external meetings
# ---------------------------------------------------------------------------


def get_upcoming_external_meetings() -> list[dict]:
    now = datetime.now(timezone.utc)
    window = now + timedelta(minutes=LOOKAHEAD_MIN)
    ae_domain = AE_EMAIL.split("@")[-1].lower()

    log.debug("%s  Fetching calendar events for next %d min", ICONS["cal"], LOOKAHEAD_MIN)
    events_data = _tool(
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

        start_str = (
            (ev.get("start") or {}).get("dateTime")
            or (ev.get("start") or {}).get("date", "")
        )
        if not start_str:
            continue

        try:
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        except ValueError:
            continue

        minutes_until = (start_dt - now).total_seconds() / 60
        if minutes_until < BRIEF_BEFORE_MIN:
            log.debug("%s  Skipping '%s' — starts in %d min (below threshold %d)",
                      ICONS["skip"], ev.get("summary", "?"), int(minutes_until), BRIEF_BEFORE_MIN)
            continue

        meetings.append({
            "id":              ev.get("id", ""),
            "title":           ev.get("summary") or "Untitled Meeting",
            "start_dt":        start_dt,
            "minutes_until":   minutes_until,
            "external_emails": [a["email"] for a in external],
        })

    return meetings

# ---------------------------------------------------------------------------
# Step 2: Granola — past meeting notes + transcripts
# ---------------------------------------------------------------------------


def get_granola_history(attendee_emails: list[str], company_name: str) \
        -> tuple[list[dict], list[str]]:
    notes: list[dict] = []
    transcripts: list[str] = []
    seen_ids: set[str] = set()

    def _extract_content_text(data: dict) -> str:
        """Extract plain text from Granola's MCP content[] wrapper."""
        blocks = data.get("content") or []
        parts = []
        for block in blocks:
            text = block.get("text", "")
            if text and "count=\"0\"" not in text and "no meeting" not in text.lower():
                parts.append(text.strip())
        return "\n".join(parts)

    queries = ([*attendee_emails, company_name] if company_name else attendee_emails)[:3]
    for query in queries:
        if not query:
            continue
        data = _tool(GRANOLA_CONNECTOR, "granolamcp_query_granola_meetings",
                     query=query, limit=5)
        if not data:
            log.debug("%s  Granola: empty response for '%s'", ICONS["skip"], query)
            continue

        raw_meetings = data.get("meetings") or data.get("results") or []

        if not raw_meetings:
            # MCP content[] wrapper — check for no-data signal
            content_blocks = data.get("content") or []
            has_data = any(
                "count=\"0\"" not in b.get("text", "") and
                "no meeting" not in b.get("text", "").lower()
                for b in content_blocks
                if b.get("text")
            )
            if not has_data:
                log.debug("%s  Granola: no meetings synced for '%s'", ICONS["skip"], query)
            continue

        for m in raw_meetings:
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
            data = _tool(GRANOLA_CONNECTOR, "granolamcp_get_meeting_transcript",
                         meeting_id=m_id)
            if data:
                raw = data.get("transcript") or data.get("notes") or data.get("summary") or ""
                if isinstance(raw, list):
                    tx = "\n".join(
                        str(t.get("text") or t.get("content") or t) for t in raw
                    )
                elif raw:
                    tx = str(raw)
                else:
                    # Also check content[] wrapper
                    tx = _extract_content_text(data)
            if tx:
                log.debug("%s  Granola: got transcript (%d chars) for meeting %s",
                          ICONS["notes"], len(tx), m_id)
        transcripts.append(tx)

    while len(transcripts) < len(notes):
        transcripts.append("")

    return notes, transcripts

# ---------------------------------------------------------------------------
# Step 3: Attio — deal lookup
# ---------------------------------------------------------------------------


def get_attio_deal(attendee_emails: list[str]) -> dict:
    attendee_domains = {e.split("@")[-1].lower() for e in attendee_emails}

    def _first(vals: dict, key: str) -> str:
        """Extract a scalar string from an Attio values dict field."""
        v = vals.get(key, [])
        if not isinstance(v, list) or not v:
            return str(v) if v else ""
        item = v[0]
        if isinstance(item.get("status"), dict):
            return item["status"].get("title", "")
        val = item.get("value")
        if val is not None:
            return str(val)
        return item.get("text") or item.get("name") or item.get("full_name") or ""

    def _parse_deal(d: dict) -> dict:
        vals = d.get("values", {})
        record_id = (d.get("id") or {}).get("record_id") or str(d.get("id") or "")
        deal_name = _first(vals, "name") or _first(vals, "deal_name")
        if not deal_name and attendee_emails:
            deal_name = attendee_emails[0].split("@")[-1].split(".")[0].title()
        return {
            "id":            record_id,
            "name":          deal_name,
            "stage":         _first(vals, "stage"),
            "value":         _first(vals, "value") or "0",
            "last_activity": _first(vals, "last_activity_date") or _first(vals, "last_contact_date"),
        }

    def _deal_matches_domain(d: dict) -> bool:
        """True if deal name contains any attendee domain keyword."""
        name = _first(d.get("values", {}), "name").lower()
        for domain in attendee_domains:
            keyword = domain.split(".")[0]
            if keyword and keyword in name:
                return True
        return False

    seen_ids: set[str] = set()

    # Pass 1: search deals directly by email + domain keyword
    queries: list[str] = []
    for email in attendee_emails:
        queries.append(email)
    for email in attendee_emails:
        kw = email.split("@")[-1].split(".")[0]
        if kw not in queries:
            queries.append(kw)

    for query in queries[:4]:
        try:
            deal_data = _tool("attio", "attio_search_records",
                              object="deals", query=query, limit=5)
        except Exception as exc:
            log.debug("%s  Attio deal search '%s' failed: %s", ICONS["warn"], query, exc)
            continue
        for d in (deal_data.get("data") or []):
            rid = (d.get("id") or {}).get("record_id", "")
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            if _deal_matches_domain(d):
                log.debug("%s  Attio: matched deal by name for '%s'", ICONS["deal"], query)
                return _parse_deal(d)
            log.debug("%s  Attio: deal '%s' doesn't match domain — skipping",
                      ICONS["skip"], _first(d.get("values", {}), "name"))

    # Pass 2: look up company by domain → find deals linked to that company
    for domain in attendee_domains:
        try:
            company_data = _tool("attio", "attio_search_records",
                                 object="companies", query=domain, limit=3)
        except Exception as exc:
            log.debug("%s  Attio company search '%s' failed: %s", ICONS["warn"], domain, exc)
            continue
        for company in (company_data.get("data") or []):
            c_vals = company.get("values", {})
            company_domains = [
                (entry.get("domain") or "").lower()
                for entry in (c_vals.get("domains") or [])
                if entry.get("domain")
            ]
            kw = domain.split(".")[0].lower()
            domain_match = any(domain in cd or kw in cd for cd in company_domains)
            name_match = kw in _first(c_vals, "name").lower()
            if not domain_match and not name_match:
                continue
            c_name = _first(c_vals, "name")
            log.debug("%s  Attio: found company '%s' for domain '%s'",
                      ICONS["deal"], c_name, domain)
            # Search deals by company name
            try:
                deal_data = _tool("attio", "attio_search_records",
                                  object="deals", query=c_name, limit=5)
            except Exception as exc:
                log.debug("%s  Attio deal search by company '%s' failed: %s",
                          ICONS["warn"], c_name, exc)
                continue
            for d in (deal_data.get("data") or []):
                rid = (d.get("id") or {}).get("record_id", "")
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)
                log.debug("%s  Attio: matched deal via company lookup for '%s'",
                          ICONS["deal"], c_name)
                return _parse_deal(d)

    return {}

# ---------------------------------------------------------------------------
# Step 4: Claude — synthesize prep brief
# ---------------------------------------------------------------------------


def synthesize_brief(
    meeting_title: str,
    attendees: list[str],
    granola_notes: list[dict],
    deal: dict,
    transcripts: list[str],
) -> str:
    import anthropic

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

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()

# ---------------------------------------------------------------------------
# Step 5: Slack — send DM
# ---------------------------------------------------------------------------


def send_slack_brief(event: dict, brief: str, deal: dict, granola_notes: list[dict]) -> None:
    now = datetime.now(timezone.utc)
    mins = int((event["start_dt"] - now).total_seconds() / 60)

    deal_line = (
        f"\n{ICONS['deal']} *Deal:* {deal.get('name', 'Unknown')} | "
        f"Stage: {deal.get('stage', 'N/A')} | "
        f"Value: ${deal.get('value', '0')}"
        if deal else ""
    )
    granola_line = (
        f"\n{ICONS['notes']} *Prior meetings:* {len(granola_notes)} found in Granola"
        if granola_notes else ""
    )

    message = (
        f"{ICONS['cal']} *Call Prep Brief — {event['title']}*\n"
        f"Starting in *{mins} minutes* · {event['start_dt'].strftime('%I:%M %p %Z')}"
        f"{deal_line}"
        f"\n👥 *Attendees:* {', '.join(event['external_emails'])}"
        f"{granola_line}"
        f"\n\n{brief}"
        f"\n\n_Powered by Google Calendar + Granola + Attio + Scalekit Agent Auth_"
    )

    if SLACK_CONNECTOR in _unavailable:
        log.warning("%s  Slack unavailable — printing brief to stdout instead",
                    ICONS["warn"])
        print("\n" + "─" * 60)
        print(message)
        print("─" * 60 + "\n")
        return

    client = _get_client()
    result = client.actions.execute_tool(
        tool_name="slack_send_message",
        identifier=CONNECTOR_USERS[SLACK_CONNECTOR],
        tool_input={"channel": SLACK_DM_USER, "text": message},
    )
    ts = (result.data or {}).get("timestamp", "")
    log.info("%s  Brief sent for '%s' (ts=%s)", ICONS["slack"], event["title"], ts or "?")

# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------


def process_cycle() -> None:
    log.info("%s  Poll cycle: %s", ICONS["poll"],
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    log.info("%s  Checking upcoming calendar events (next %d min)...",
             ICONS["cal"], LOOKAHEAD_MIN)
    meetings = get_upcoming_external_meetings()
    log.info("%s  Found %d external meeting(s)", ICONS["cal"], len(meetings))

    if not meetings:
        log.info("%s  No meetings requiring briefs", ICONS["skip"])
        return

    for event in meetings:
        if event["id"] in _sent_event_ids:
            log.info("%s  Brief already sent for '%s' — skipping",
                     ICONS["skip"], event["title"])
            continue

        log.info("%s  '%s' | starts in %d min | attendees: %s",
                 ICONS["cal"], event["title"],
                 int(event["minutes_until"]),
                 ", ".join(event["external_emails"]))

        company_name = (
            event["external_emails"][0].split("@")[-1].split(".")[0].capitalize()
            if event["external_emails"] else ""
        )

        log.info("%s  Pulling Granola meeting history...", ICONS["notes"])
        granola_notes, transcripts = get_granola_history(
            event["external_emails"], company_name
        )
        log.info("%s  %d prior meeting(s) found in Granola",
                 ICONS["notes"], len(granola_notes))

        log.info("%s  Looking up Attio deal...", ICONS["deal"])
        deal = get_attio_deal(event["external_emails"])
        if deal:
            log.info("%s  Deal: %s | Stage: %s | Value: $%s",
                     ICONS["deal"],
                     deal.get("name", "?"),
                     deal.get("stage", "?"),
                     deal.get("value", "0"))
        else:
            log.info("%s  No deal found in Attio — new prospect", ICONS["skip"])

        log.info("%s  Synthesizing prep brief via Claude...", ICONS["brief"])
        try:
            brief = synthesize_brief(
                meeting_title=event["title"],
                attendees=event["external_emails"],
                granola_notes=granola_notes,
                deal=deal,
                transcripts=transcripts,
            )
            log.info("%s  Brief ready (%d chars)", ICONS["brief"], len(brief))
        except Exception as exc:
            log.error("%s  Brief generation failed: %s", ICONS["error"], exc)
            continue

        log.info("%s  Sending Slack brief to %s...", ICONS["slack"], SLACK_DM_USER)
        try:
            send_slack_brief(event, brief, deal, granola_notes)
            _sent_event_ids.add(event["id"])
        except Exception as exc:
            log.error("%s  Slack send failed: %s", ICONS["error"], exc)

    log.info("%s  Poll cycle complete", ICONS["done"])

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    _banner()

    log.info("%s  Checking Scalekit connector auth...", ICONS["auth"])
    for connector in ("googlecalendar", GRANOLA_CONNECTOR, "attio", SLACK_CONNECTOR):
        ensure_authorized(connector)

    if "googlecalendar" in _unavailable:
        log.error("%s  Google Calendar unavailable — cannot continue", ICONS["error"])
        sys.exit(1)

    if POLLING_MODE:
        log.info("%s  Polling every %d min (Ctrl+C to stop)",
                 ICONS["poll"], POLL_INTERVAL_MIN)
        consecutive_errors = 0
        while True:
            try:
                process_cycle()
                consecutive_errors = 0
            except KeyboardInterrupt:
                log.info("%s  Interrupted — exiting", ICONS["done"])
                break
            except Exception as exc:
                consecutive_errors += 1
                log.error("%s  Cycle error (%d consecutive): %s",
                          ICONS["error"], consecutive_errors, exc)
                if consecutive_errors >= 5:
                    log.error("%s  5 consecutive errors — check connectivity and credentials",
                              ICONS["error"])
            log.info("%s  Next poll in %d min...", ICONS["poll"], POLL_INTERVAL_MIN)
            time.sleep(POLL_INTERVAL_MIN * 60)
    else:
        process_cycle()


if __name__ == "__main__":
    main()
