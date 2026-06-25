# runner.py — Gmail Scheduling Agent poll loop

from __future__ import annotations
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta

import pytz

from sk_connectors import ensure_connected
from gmail_api import fetch_emails, get_message, create_draft, mark_read
from calendar_api import get_primary_calendar_id, create_event, get_busy_slots
from parsers import parse_entities
from slotting import suggest_slots, human_slot, iso

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

ICONS = {
    "poll":    "⌕",
    "email":   "📧",
    "cal":     "📅",
    "done":    "✔",
    "skip":    "–",
    "warn":    "⚠",
    "error":   "✖",
    "llm":     "✦",
    "draft":   "✉",
    "banner":  "─",
}

CYAN  = "\033[36m"
BOLD  = "\033[1m"
WHITE = "\033[97m"
GREY  = "\033[90m"
RESET = "\033[0m"


def _setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    fmt = "%(asctime)s | %(levelname)-8s | %(message)s"
    datefmt = "%H:%M:%S"
    logging.basicConfig(level=getattr(logging, level, logging.INFO),
                        format=fmt, datefmt=datefmt, stream=sys.stdout)
    # Suppress noisy SDK internals
    for noisy in ("httpx", "httpcore", "urllib3", "grpc"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_setup_logging()
log = logging.getLogger("scheduling-agent")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

USER_TZ          = os.getenv("USER_DEFAULT_TZ", "Asia/Kolkata")
LOCAL_TZ         = pytz.timezone(USER_TZ)
WORK_START       = os.getenv("WORK_START_LOCAL", "10:00")
WORK_END         = os.getenv("WORK_END_LOCAL", "18:00")
DEFAULT_DURATION = int(os.getenv("DEFAULT_DURATION_MIN", "30"))
BUFFER_MIN       = int(os.getenv("BUFFER_MIN", "10"))
POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
IDENTIFIER       = os.getenv("SCALEKIT_IDENTIFIER", "")

# Gmail search — catches plain-text scheduling requests AND .ics invites
GMAIL_QUERY = (
    "is:unread "
    "(subject:(meeting OR sync OR call OR invite OR schedule OR \"let's meet\" OR \"catch up\") "
    "OR body:(\"can we meet\" OR \"let's sync\" OR \"schedule a call\" OR \"set up a meeting\" "
    "        OR \"book a time\" OR \"find a time\" OR \"calendar invite\") "
    "OR has:attachment filename:ics)"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hm(hm_str: str):
    h, m = hm_str.split(":")
    return datetime.strptime(f"{h}:{m}", "%H:%M").time()


def _headers_dict(message: dict) -> dict:
    hdrs = {}
    for h in (message.get("payload") or {}).get("headers", []):
        if h.get("name"):
            hdrs[h["name"]] = h.get("value", "")
    return hdrs


def _reply_to(headers: dict) -> str:
    """Extract bare email address from Reply-To or From header."""
    raw = headers.get("Reply-To") or headers.get("From") or ""
    # Strip display name: "Name <email>" → "email"
    m = re.search(r"<([^>]+)>", raw)
    return m.group(1).strip() if m else raw.strip()

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

def _banner() -> None:
    tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    if not tty:
        print("Gmail Scheduling Agent starting...")
        return
    line = f"{CYAN}{BOLD}{'─' * 60}{RESET}"
    print(line)
    print(f"{CYAN}{BOLD}  Gmail Scheduling Agent{RESET}")
    print(f"{GREY}  Watches Gmail and books Google Calendar events automatically{RESET}")
    print(line)
    masked = ("*" * 4 + IDENTIFIER[-4:]) if len(IDENTIFIER) > 4 else "****"
    print(f"  {GREY}Identifier   :{RESET} {WHITE}{masked}{RESET}")
    print(f"  {GREY}Timezone     :{RESET} {WHITE}{USER_TZ}{RESET}")
    print(f"  {GREY}Work hours   :{RESET} {WHITE}{WORK_START} – {WORK_END}{RESET}")
    print(f"  {GREY}Poll interval:{RESET} {WHITE}{POLL_INTERVAL}s{RESET}")
    print(line)
    print()

# ---------------------------------------------------------------------------
# Core: process one email
# ---------------------------------------------------------------------------

def process_message(identifier: str, msg_stub: dict, processed: set) -> bool:
    msg_id = msg_stub.get("id")
    if not msg_id or msg_id in processed:
        return False

    full = get_message(identifier, msg_id)
    if not full:
        log.warning("%s  Could not fetch message %s", ICONS["warn"], msg_id)
        return False

    headers = _headers_dict(full)
    subject = next(
        (h["value"] for h in (full.get("payload") or {}).get("headers", [])
         if h.get("name") == "Subject"), ""
    ) or "(no subject)"
    body = full.get("snippet") or ""

    log.info("%s  %s", ICONS["email"], subject[:80])

    # LLM extracts intent, datetime, attendees
    log.debug("%s  Parsing email for scheduling intent...", ICONS["llm"])
    ent = parse_entities(subject, body, headers, user_tz=USER_TZ, default_duration=DEFAULT_DURATION)

    if not ent.hard_start:
        log.info("%s  No scheduling intent found — skipping", ICONS["skip"])
        try:
            mark_read(identifier, msg_id)
        except Exception as exc:
            log.warning("%s  Could not mark message %s as read: %s",
                        ICONS["warn"], msg_id, exc)
        processed.add(msg_id)
        return False

    log.info("%s  Intent: '%s' | %s | %dmin",
             ICONS["cal"], ent.title,
             ent.hard_start.strftime("%a %b %d %H:%M %Z"),
             ent.duration_minutes)

    # Free/busy check
    cal_id = get_primary_calendar_id(identifier)
    now = datetime.now(LOCAL_TZ)
    busy = get_busy_slots(identifier, iso(now - timedelta(days=1)),
                          iso(now + timedelta(days=30)), LOCAL_TZ)
    log.debug("%s  %d busy block(s) on calendar", ICONS["cal"], len(busy))

    proposed_start = ent.hard_start.astimezone(LOCAL_TZ)
    proposed_end = (
        ent.hard_end.astimezone(LOCAL_TZ) if ent.hard_end
        else proposed_start + timedelta(minutes=ent.duration_minutes)
    )

    conflict = any(proposed_start < b1 and proposed_end > b0 for b0, b1 in busy)

    if conflict:
        log.info("%s  Conflict at proposed time — finding alternatives", ICONS["warn"])
        free_slots = suggest_slots(
            busy=busy, now_local=now,
            work_start=_hm(WORK_START), work_end=_hm(WORK_END),
            duration_min=ent.duration_minutes, buffer_min=BUFFER_MIN,
            days_ahead=7, limit=3,
        )
        if not free_slots:
            log.warning("%s  No free slots in next 7 days — skipping", ICONS["warn"])
            processed.add(msg_id)
            return False
        proposed_start, proposed_end = free_slots[0]
        log.info("%s  Rescheduled → %s", ICONS["cal"], human_slot(free_slots[0], USER_TZ))

    # Create calendar event
    event_payload = {
        "summary": ent.title,
        "start":   {"dateTime": iso(proposed_start), "timeZone": USER_TZ},
        "end":     {"dateTime": iso(proposed_end),   "timeZone": USER_TZ},
        "attendees": [a.model_dump() for a in ent.attendees],
        "sendUpdates": "all",
    }
    result = create_event(identifier, cal_id, event_payload)

    if "error" in result:
        log.error("%s  Event creation failed: %s", ICONS["error"], result["error"])
        processed.add(msg_id)
        return False

    event_link = result.get("htmlLink", "")
    log.info("%s  Event created: %s", ICONS["done"], event_link or "(no link)")

    # Save confirmation draft
    reply_to = _reply_to(headers)
    if reply_to:
        slot_str = human_slot((proposed_start, proposed_end), USER_TZ)
        draft_body = (
            f"Hi,\n\n"
            f"I've scheduled '{ent.title}' for {slot_str}.\n"
            f"A calendar invite has been sent to all attendees.\n"
            + (f"\nEvent: {event_link}\n" if event_link else "")
            + "\nBest regards"
        ).encode("ascii", errors="replace").decode("ascii")
        draft_subject = f"Re: {subject}"[:100]
        try:
            draft = create_draft(identifier, to=reply_to,
                                 subject=draft_subject, body=draft_body)
            log.info("%s  Confirmation draft saved (id=%s)",
                     ICONS["draft"], draft.get("id", "?"))
        except Exception as exc:
            log.warning("%s  Draft creation failed for message %s: %s",
                        ICONS["warn"], msg_id, exc)

    try:
        mark_read(identifier, msg_id)
    except Exception as exc:
        log.warning("%s  Could not mark message %s as read: %s",
                    ICONS["warn"], msg_id, exc)
    processed.add(msg_id)
    log.info("%s  Message %s done", ICONS["done"], msg_id)
    return True

# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------

def main() -> None:
    _banner()

    if not IDENTIFIER:
        log.error("%s  SCALEKIT_IDENTIFIER not set in .env", ICONS["error"])
        sys.exit(1)

    log.info("Checking Scalekit connections...")
    ensure_connected("gmail", IDENTIFIER)
    ensure_connected("googlecalendar", IDENTIFIER)
    log.info("%s  Both connections active", ICONS["done"])

    processed: set = set()
    consecutive_errors = 0

    while True:
        try:
            log.info("%s  Polling Gmail for scheduling emails...", ICONS["poll"])
            msgs = fetch_emails(IDENTIFIER, GMAIL_QUERY, max_results=10)

            if msgs:
                log.info("%s  Found %d unread email(s) to process", ICONS["poll"], len(msgs))
                done = 0
                for m in msgs:
                    try:
                        if process_message(IDENTIFIER, m, processed):
                            done += 1
                    except Exception as msg_exc:
                        log.error("%s  Failed processing message %s: %s",
                                  ICONS["error"], m.get("id", "?"), msg_exc)
                log.info("%s  Poll complete — processed=%d skipped=%d",
                         ICONS["done"], done, len(msgs) - done)
            else:
                log.info("%s  No new scheduling emails", ICONS["skip"])

            consecutive_errors = 0

        except Exception as exc:
            consecutive_errors += 1
            log.error("%s  Poll error (%d consecutive): %s",
                      ICONS["error"], consecutive_errors, exc)
            if consecutive_errors >= 5:
                log.error("%s  5 consecutive errors — check connectivity and credentials",
                          ICONS["error"])

        log.info("%s  Next poll in %ds...", ICONS["poll"], POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
