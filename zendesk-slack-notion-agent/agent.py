import os
import sys
import json
import time
import logging
from typing import List, Dict, Any

from scalekit import ScalekitClient
from google import genai as _genai

import settings
settings.validate()

if os.getenv("GOOGLE_ADK_API_KEY") and not os.getenv("GOOGLE_API_KEY"):
    os.environ["GOOGLE_API_KEY"] = os.getenv("GOOGLE_ADK_API_KEY")

# ---------------------------------------------------------------------------
# Colored logger
# ---------------------------------------------------------------------------

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RED    = "\033[91m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
WHITE  = "\033[97m"
GREY   = "\033[90m"

LEVEL_COLORS = {
    "DEBUG":    GREY,
    "INFO":     CYAN,
    "WARNING":  YELLOW,
    "ERROR":    RED,
    "CRITICAL": RED + BOLD,
}

STEP_ICONS = {
    "fetch":   "↓",
    "reply":   "✦",
    "slack":   "→",
    "notion":  "◈",
    "start":   "▶",
    "done":    "✔",
    "skip":    "–",
    "warn":    "⚠",
    "error":   "✖",
}


class ColorFormatter(logging.Formatter):
    """Format log records with ANSI colors when writing to a TTY."""

    def __init__(self, colorize: bool = True):
        """Initialize formatter, enabling ANSI colors only when output is a TTY."""
        super().__init__()
        self.colorize = colorize

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record with optional ANSI color codes."""
        ts    = self.formatTime(record, "%H:%M:%S")
        level = record.levelname
        msg   = record.getMessage()

        if not self.colorize:
            return f"{ts} | {level:<8} | {msg}"

        col = LEVEL_COLORS.get(level, WHITE)
        ts_str    = f"{GREY}{ts}{RESET}"
        level_str = f"{col}{BOLD}{level:<8}{RESET}"
        msg_str   = f"{WHITE}{msg}{RESET}" if level == "INFO" else f"{col}{msg}{RESET}"
        return f"{ts_str} {DIM}|{RESET} {level_str} {DIM}|{RESET} {msg_str}"


class _NoiseFilter(logging.Filter):
    _SKIP = ("AFC is enabled",)

    def filter(self, record: logging.LogRecord) -> bool:
        return not any(s in record.getMessage() for s in self._SKIP)


def _setup_logging() -> None:
    """Configure root logger with color console output; suppress noisy third-party loggers."""
    is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColorFormatter(colorize=is_tty))
    handler.addFilter(_NoiseFilter())
    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.LOG_LEVEL, logging.INFO))
    root.handlers = [handler]
    for noisy in ("httpx", "httpcore", "google.adk", "grpc", "urllib3",
                  "google.genai", "google.generativeai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_setup_logging()
logger = logging.getLogger("support-agent")


def _banner() -> None:
    """Print a startup banner to stdout."""
    tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    if tty:
        line = f"{CYAN}{BOLD}{'─' * 58}{RESET}"
        print(line)
        print(f"{CYAN}{BOLD}  Support Ticket Automation Agent{RESET}")
        print(f"{GREY}  Zendesk → Gemini → Slack → Notion via Scalekit Actions{RESET}")
        print(line)
        print(f"  {GREY}Environment :{RESET} {WHITE}{settings.SCALEKIT_ENV_URL}{RESET}")
        print(f"  {GREY}Model       :{RESET} {WHITE}{settings.GOOGLE_ADK_MODEL}{RESET}")
        print(f"  {GREY}Poll every  :{RESET} {WHITE}{settings.POLL_INTERVAL}s{RESET}")
        print(f"  {GREY}State file  :{RESET} {WHITE}state/ticket_thread_map.json{RESET}")
        print(line)
        print(f"  {YELLOW}Press Ctrl+C to stop.{RESET}")
        print(line)
        print()
    else:
        print("Support Ticket Automation Agent starting...")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

STATE_FILE = "state/ticket_thread_map.json"


def _load_state() -> dict:
    """Load idempotency map from disk, returning empty dict on any error."""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_state(mapping: dict) -> None:
    """Persist the idempotency map to disk."""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(mapping, f, indent=2)


ticket_thread_map = _load_state()

# ---------------------------------------------------------------------------
# Scalekit client + Gemini client
# ---------------------------------------------------------------------------

sk = ScalekitClient(
    env_url=settings.SCALEKIT_ENV_URL,
    client_id=settings.SCALEKIT_CLIENT_ID,
    client_secret=settings.SCALEKIT_CLIENT_SECRET,
)

_genai_client = _genai.Client(api_key=settings.GOOGLE_API_KEY)


def _generate_reply(ticket: dict) -> str:
    """Call Gemini directly to draft a concise reply for one ticket."""
    prompt = (
        "You are a helpful support agent. Write only the suggested reply — no bullet points, "
        "no meta-commentary. Keep it concise and ready to send to the customer.\n\n"
        f"Subject: {ticket.get('subject', '')}\n"
        f"Description: {ticket.get('description', '')}"
    )
    try:
        resp = _genai_client.models.generate_content(
            model=settings.GOOGLE_ADK_MODEL,
            contents=prompt,
        )
        return resp.text.strip()
    except Exception as exc:
        short = str(exc).split("\n")[0][:120]
        logger.warning("%s Reply generation failed for #%s: %s",
                       STEP_ICONS["warn"], ticket.get("id"), short)
    return "Thank you for reaching out! We are looking into your issue."


# ---------------------------------------------------------------------------
# Tool functions (exposed to ADK orchestrator)
# ---------------------------------------------------------------------------

def fetch_new_tickets() -> dict:
    """Fetch new Zendesk tickets via Scalekit Actions and return a trimmed list."""
    logger.info("%s  Fetching Zendesk tickets...", STEP_ICONS["fetch"])
    try:
        resp = sk.actions.execute_tool(
            tool_name="zendesk_tickets_list",
            identifier=settings.ZENDESK_IDENTIFIER,
            connection_name=settings.ZENDESK_CONNECTION_NAME,
            tool_input={"status": "new", "sort_by": "created_at", "sort_order": "asc"},
        )
        raw = (resp.data or {}).get("tickets", [])
        # Keep only fields needed downstream; large payloads cause MALFORMED_FUNCTION_CALL
        tickets = [
            {
                "id":          t.get("id"),
                "subject":     t.get("subject", ""),
                "description": t.get("description", ""),
                "status":      t.get("status", ""),
            }
            for t in raw
        ]
        logger.info("%s  Fetched %d ticket(s) from Zendesk.", STEP_ICONS["done"], len(tickets))
        return {"tickets": tickets}
    except Exception as exc:
        logger.error("%s  Zendesk fetch failed: %s", STEP_ICONS["error"], exc)
        return {"tickets": []}


def annotate_tickets_with_replies(tickets: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Generate a Gemini-powered suggested reply for each ticket."""
    if not tickets:
        return {"tickets": []}
    logger.info("%s  Generating replies for %d ticket(s) via Gemini...",
                STEP_ICONS["reply"], len(tickets))
    annotated = []
    for t in tickets:
        t = dict(t)
        reply = _generate_reply(t)
        t["suggested_reply"] = reply
        logger.info("    #%s: %s", t["id"], reply[:80] + ("…" if len(reply) > 80 else ""))
        annotated.append(t)
    return {"tickets": annotated}


def post_slack_digest(context: dict) -> dict:
    """Post a digest of new tickets to Slack. Each ticket is posted at most once."""
    tickets = context.get("tickets", [])
    fresh = [t for t in tickets if str(t["id"]) not in ticket_thread_map]
    if not fresh:
        logger.info("%s  Slack digest skipped — all tickets already posted.", STEP_ICONS["skip"])
        return {"status": "skipped"}

    lines = [
        f"• #{t['id']}: {t.get('subject', 'No subject')}\n"
        f"  _{t.get('suggested_reply', '')[:120]}_"
        for t in fresh
    ]
    text = "*New support tickets digest*\n\n" + "\n\n".join(lines)

    logger.info("%s  Posting Slack digest (%d ticket(s))...", STEP_ICONS["slack"], len(fresh))
    try:
        sk.actions.execute_tool(
            tool_name="slack_send_message",
            identifier=settings.SLACK_IDENTIFIER,
            connection_name=settings.SLACK_CONNECTION_NAME,
            tool_input={"channel": settings.SLACK_SUPPORT_CHANNEL, "text": text},
        )
        for t in fresh:
            ticket_thread_map[str(t["id"])] = "digest"
        _save_state(ticket_thread_map)
        logger.info("%s  Slack digest posted to %s.",
                    STEP_ICONS["done"], settings.SLACK_SUPPORT_CHANNEL)
        return {"status": "posted"}
    except Exception as exc:
        logger.error("%s  Slack digest failed: %s", STEP_ICONS["error"], exc)
        return {"status": "error", "error": str(exc)}


def _classify_ticket(subject: str) -> str:
    """Return a simple category string from the ticket subject."""
    s = subject.lower()
    if any(w in s for w in ("login", "password", "auth", "account", "sign")):
        return "Account"
    if any(w in s for w in ("pay", "bill", "invoice", "charge", "refund")):
        return "Billing"
    if any(w in s for w in ("bug", "error", "crash", "broken", "fail")):
        return "Bug"
    if any(w in s for w in ("feature", "request", "suggestion", "improve")):
        return "Feature Request"
    return "General"


def save_to_notion_kb(context: dict) -> dict:
    """Save each ticket as a row in the Notion knowledge base."""
    tickets = context.get("tickets", [])
    saved = 0
    for t in tickets:
        ticket_id  = str(t["id"])
        notion_key = f"notion_{ticket_id}"
        if notion_key in ticket_thread_map:
            logger.info("%s  Ticket #%s already in Notion KB; skipping.",
                        STEP_ICONS["skip"], ticket_id)
            continue

        subject  = t.get("subject", f"Ticket {ticket_id}")
        reply    = t.get("suggested_reply", "")
        category = _classify_ticket(subject)

        logger.info("%s  Saving ticket #%s to Notion KB...", STEP_ICONS["notion"], ticket_id)
        try:
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
            ticket_thread_map[notion_key] = True
            _save_state(ticket_thread_map)
            saved += 1
            logger.info("%s  Notion KB row created for ticket #%s.", STEP_ICONS["done"], ticket_id)
        except Exception as exc:
            msg = str(exc)
            # extract the readable Notion error message from the gRPC envelope
            import re as _re
            m = _re.search(r'"message":"([^"]+)"', msg)
            short = m.group(1) if m else msg.split("\n")[0]
            logger.error("%s  Notion save failed for ticket #%s: %s — "
                         "create Name/Ticket ID/Category/Reply columns in the DB first.",
                         STEP_ICONS["error"], ticket_id, short)
    return {"saved": saved}


# ---------------------------------------------------------------------------
# Pipeline runner — deterministic, no LLM routing between steps
# ---------------------------------------------------------------------------

def run_pipeline() -> None:
    """Execute one full poll cycle: fetch -> reply -> slack -> notion."""
    result = fetch_new_tickets()
    tickets = result.get("tickets", [])

    if not tickets:
        logger.info("%s  No new tickets this cycle.", STEP_ICONS["skip"])
        return

    annotated = annotate_tickets_with_replies(tickets)
    context   = {"tickets": annotated.get("tickets", [])}

    post_slack_digest(context)
    save_to_notion_kb(context)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the support workflow loop, polling every POLL_INTERVAL seconds."""
    _banner()
    cycle = 0
    while True:
        cycle += 1
        tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
        sep = f"{GREY}{'·' * 58}{RESET}" if tty else "·" * 58
        print(sep)
        logger.info("%s  Cycle #%d", STEP_ICONS["start"], cycle)
        try:
            run_pipeline()
        except KeyboardInterrupt:
            print()
            logger.info("%s  Shutting down. Goodbye.", STEP_ICONS["done"])
            break
        except Exception as exc:
            logger.error("%s  Cycle failed: %s", STEP_ICONS["error"], exc)
        logger.info("%s  Next cycle in %ds...", STEP_ICONS["skip"], settings.POLL_INTERVAL)
        try:
            time.sleep(settings.POLL_INTERVAL)
        except KeyboardInterrupt:
            print()
            logger.info("%s  Shutting down. Goodbye.", STEP_ICONS["done"])
            break


if __name__ == "__main__":
    main()
