"""
Freshdesk CSAT Follow-up Agent
Watches resolved Freshdesk tickets, reads CSAT survey results, and uses
Google ADK (Gemini) to decide whether to thank and close or reopen and apologize.
All Freshdesk actions go through Scalekit agent auth.
"""

import os
import sys
import json
import time
import asyncio
import logging
import pathlib

# ---------------------------------------------------------------------------
# Force native DNS resolver before any network imports (required on Python 3.13 + macOS)
os.environ.setdefault("GRPC_DNS_RESOLVER", "native")

from dotenv import load_dotenv
load_dotenv()

# Alias GOOGLE_ADK_API_KEY -> GOOGLE_API_KEY (ADK reads GOOGLE_API_KEY)
if os.getenv("GOOGLE_ADK_API_KEY") and not os.getenv("GOOGLE_API_KEY"):
    os.environ["GOOGLE_API_KEY"] = os.environ["GOOGLE_ADK_API_KEY"]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RED    = "\033[91m"
YELLOW = "\033[93m"
GREEN  = "\033[92m"
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

ICONS = {
    "start":  "▶",
    "done":   "✔",
    "warn":   "⚠",
    "error":  "✖",
    "skip":   "–",
    "ticket": "🎫",
    "survey": "★",
    "action": "⚡",
    "llm":    "✦",
    "poll":   "⌕",
}


class _ColorFormatter(logging.Formatter):
    def __init__(self, colorize: bool = True):
        super().__init__()
        self.colorize = colorize

    def format(self, record: logging.LogRecord) -> str:
        ts    = self.formatTime(record, "%H:%M:%S")
        level = record.levelname
        msg   = record.getMessage()
        if not self.colorize:
            return f"{ts} | {level:<8} | {msg}"
        col     = LEVEL_COLORS.get(level, WHITE)
        ts_str  = f"{GREY}{ts}{RESET}"
        lv_str  = f"{col}{BOLD}{level:<8}{RESET}"
        msg_str = f"{WHITE}{msg}{RESET}" if level == "INFO" else f"{col}{msg}{RESET}"
        return f"{ts_str} {DIM}|{RESET} {lv_str} {DIM}|{RESET} {msg_str}"


class _NoiseFilter(logging.Filter):
    _SKIP = ("AFC is enabled", "grpc", "urllib3")

    def filter(self, record: logging.LogRecord) -> bool:
        return not any(s in record.getMessage() for s in self._SKIP)


def _setup_logging() -> None:
    is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_ColorFormatter(colorize=is_tty))
    handler.addFilter(_NoiseFilter())
    root = logging.getLogger()
    root.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO"), logging.INFO))
    root.handlers = [handler]
    for noisy in ("httpx", "httpcore", "grpc", "urllib3", "google.auth",
                  "google.genai", "opentelemetry", "google.adk"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_setup_logging()
log = logging.getLogger("freshdesk-agent")

# ---------------------------------------------------------------------------
# Settings + validation
# ---------------------------------------------------------------------------

SCALEKIT_ENV_URL       = os.getenv("SCALEKIT_ENV_URL", "")
SCALEKIT_CLIENT_ID     = os.getenv("SCALEKIT_CLIENT_ID", "")
SCALEKIT_CLIENT_SECRET = os.getenv("SCALEKIT_CLIENT_SECRET", "")
FRESHDESK_IDENTIFIER   = os.getenv("FRESHDESK_IDENTIFIER", "")
FRESHDESK_CONNECTION   = os.getenv("FRESHDESK_CONNECTION", "freshdesk")
GOOGLE_API_KEY         = os.getenv("GOOGLE_API_KEY", "")
GEMINI_MODEL           = os.getenv("GOOGLE_ADK_MODEL", "gemini-2.0-flash")
POLL_INTERVAL          = int(os.getenv("POLL_INTERVAL", "60"))
STATE_FILE             = pathlib.Path(__file__).parent / "processed_tickets.json"

_THANK_MSG  = "Thank you for your feedback! We are glad we could help. This ticket is now closed."
_REOPEN_MSG = "We are sorry to hear that. We have reopened this ticket and will follow up to make things right."

APP_NAME = "freshdesk_csat_agent"


def validate_config() -> None:
    missing = []
    if not SCALEKIT_ENV_URL or "scalekit" not in SCALEKIT_ENV_URL:
        missing.append("SCALEKIT_ENV_URL")
    if not SCALEKIT_CLIENT_ID or SCALEKIT_CLIENT_ID == "your_scalekit_client_id":
        missing.append("SCALEKIT_CLIENT_ID")
    if not SCALEKIT_CLIENT_SECRET or SCALEKIT_CLIENT_SECRET == "your_scalekit_client_secret":
        missing.append("SCALEKIT_CLIENT_SECRET")
    if not FRESHDESK_IDENTIFIER or FRESHDESK_IDENTIFIER == "your@email.com":
        missing.append("FRESHDESK_IDENTIFIER")
    if not GOOGLE_API_KEY or GOOGLE_API_KEY in ("your_google_adk_api_key", "your_google_api_key"):
        missing.append("GOOGLE_ADK_API_KEY")
    if missing:
        raise ValueError(
            f"Missing required env vars: {', '.join(missing)}\n"
            f"Copy .env.example to .env and fill in all values."
        )


# ---------------------------------------------------------------------------
# Scalekit client (lazy init)
# ---------------------------------------------------------------------------

_sk = None


def _get_sk():
    global _sk
    if _sk is None:
        from scalekit import ScalekitClient
        _sk = ScalekitClient(
            env_url=SCALEKIT_ENV_URL,
            client_id=SCALEKIT_CLIENT_ID,
            client_secret=SCALEKIT_CLIENT_SECRET,
        )
        log.debug("Scalekit client initialised")
    return _sk


def _sk_exec(tool_name: str, params: dict) -> dict | list:
    """Execute a Freshdesk tool via Scalekit and return the data payload."""
    sk = _get_sk()
    try:
        result = sk.actions.execute_tool(
            tool_input=params,
            tool_name=tool_name,
            identifier=FRESHDESK_IDENTIFIER,
            connection_name=FRESHDESK_CONNECTION,
        )
        data = result.data or {}
        # Response may be dict (single object) or have 'array' key for lists
        if isinstance(data, dict) and "array" in data:
            return data["array"]
        return data
    except Exception as exc:
        msg = str(exc)
        # Extract the core error from Scalekit's verbose message
        if "tool_error_message" in msg:
            try:
                start = msg.index("tool_error_message") + 21
                end   = msg.index('"', start + 1)
                msg = msg[start:end].replace("\\\"", '"').replace("\\'", "'")
            except Exception:
                pass
        raise RuntimeError(f"Freshdesk {tool_name} failed: {msg[:300]}") from exc


# ---------------------------------------------------------------------------
# Freshdesk API helpers (via Scalekit)
# ---------------------------------------------------------------------------

def fd_get_tickets(page: int = 1, per_page: int = 100) -> list:
    """Fetch tickets and filter to resolved (status=4) client-side."""
    result = _sk_exec("freshdesk_tickets_list", {
        "per_page": per_page,
        "page": page,
        "include": "requester",
    })
    if not isinstance(result, list):
        raise RuntimeError(f"Unexpected tickets response type: {type(result)}")
    # Filter to resolved tickets only (status == 4)
    resolved = [t for t in result if t.get("status") == 4 or t.get("status") == 4.0]
    return resolved


def fd_get_survey(ticket_id: str) -> dict | None:
    """
    Fetch the latest CSAT survey for a ticket.
    Freshdesk's CSAT survey endpoint is not in Scalekit tools, so we use
    the Scalekit-authenticated token to call it directly via requests.
    As a fallback: check ticket's 'satisfaction_ratings' field from ticket_get.
    """
    # Try via ticket_get — some Freshdesk plans include ratings in the ticket
    try:
        ticket_data = _sk_exec("freshdesk_ticket_get", {
            "ticket_id": int(ticket_id),
            "include": "satisfaction_ratings",
        })
        ratings = None
        if isinstance(ticket_data, dict):
            ratings = ticket_data.get("satisfaction_ratings")
        if ratings and isinstance(ratings, list) and ratings:
            valid = [r for r in ratings if isinstance(r, dict) and r.get("rating") is not None]
            if valid:
                return sorted(valid, key=lambda r: r.get("created_at", ""), reverse=True)[0]
    except Exception as exc:
        log.debug("ticket_get with satisfaction_ratings failed for %s: %s", ticket_id, exc)

    # No survey found via Scalekit
    return None


def fd_reply(ticket_id: str, body: str) -> None:
    _sk_exec("freshdesk_tickets_reply", {
        "ticket_id": int(ticket_id),
        "body": body,
    })


def fd_update_status(ticket_id: str, status: int) -> None:
    _sk_exec("freshdesk_ticket_update", {
        "ticket_id": int(ticket_id),
        "status": status,
    })


# ---------------------------------------------------------------------------
# ADK client (lazy init)
# ---------------------------------------------------------------------------

_adk_agent = None


def _get_adk_agent():
    global _adk_agent
    if _adk_agent is not None:
        return _adk_agent
    try:
        from google.adk.agents import LlmAgent
        from google.adk.models.google_llm import Gemini
        _adk_agent = LlmAgent(
            name="csat_decision_agent",
            model=Gemini(model=GEMINI_MODEL),
            instruction=(
                "You are a Freshdesk CSAT decision agent. "
                "Given a ticket ID, requester email, and CSAT survey result JSON, decide what action to take. "
                "rating 103 means satisfied, -103 means not satisfied. "
                "Output ONLY valid JSON with exactly these keys: "
                '{"feedback_received": true|false, "rating": 103|-103|null, '
                '"action": "thank_and_close"|"reopen_and_apologize"|null}. '
                "No markdown, no explanation, just the JSON object."
            ),
        )
        log.info("%s  ADK agent initialised (model=%s)", ICONS["llm"], GEMINI_MODEL)
        return _adk_agent
    except Exception as exc:
        log.error("%s  Failed to initialise ADK agent: %s", ICONS["error"], exc)
        raise


async def _run_adk_async(prompt: str) -> str | None:
    """Run the ADK agent for a single prompt and return the text response."""
    from google.adk import runners
    from google.genai import types as genai_types

    agent  = _get_adk_agent()
    runner = runners.InMemoryRunner(agent=agent, app_name=APP_NAME)
    session = await runner.session_service.create_session(
        app_name=APP_NAME, user_id="freshdesk_poller"
    )
    msg = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=prompt)],
    )
    texts = []
    async for ev in runner.run_async(
        user_id="freshdesk_poller",
        session_id=session.id,
        new_message=msg,
    ):
        content = getattr(ev, "content", None)
        if not content:
            continue
        for part in getattr(content, "parts", []):
            t = getattr(part, "text", None)
            if t:
                texts.append(t)
    return "\n".join(texts) if texts else None


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```json"):
        s = s[7:]
    elif s.startswith("```"):
        s = s[3:]
    if s.endswith("```"):
        s = s[:-3]
    return s.strip()


def get_adk_decision(ticket_id: str, survey_result: dict | None, email: str | None) -> dict | None:
    """
    Ask Gemini to decide the action for this ticket.
    Returns dict with: feedback_received, rating, action — or None on failure.
    """
    prompt = (
        f"Ticket ID: {ticket_id}. "
        f"Requester: {email or 'unknown'}. "
        f"Survey Result JSON:\n{json.dumps(survey_result or {}, ensure_ascii=False)}"
    )
    log.debug("ADK prompt: %s", prompt)

    model_candidates = list(dict.fromkeys(filter(None, [
        GEMINI_MODEL,
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-2.5-pro",
    ])))

    for model in model_candidates:
        try:
            if model != GEMINI_MODEL:
                log.warning("%s  Retrying with fallback model: %s", ICONS["warn"], model)
                from google.adk.agents import LlmAgent
                from google.adk.models.google_llm import Gemini
                global _adk_agent
                _adk_agent = LlmAgent(
                    name="csat_decision_agent",
                    model=Gemini(model=model),
                    instruction=_get_adk_agent().instruction,
                )

            raw = asyncio.run(_run_adk_async(prompt))
            log.debug("ADK raw output: %s", raw)

            if not raw:
                log.warning("%s  ADK returned empty response for ticket %s", ICONS["warn"], ticket_id)
                continue

            try:
                parsed = json.loads(_strip_fences(raw))
            except json.JSONDecodeError:
                log.warning("%s  ADK response was not valid JSON for ticket %s: %s",
                            ICONS["warn"], ticket_id, raw[:200])
                parsed = {"feedback_received": False, "rating": None, "action": None}

            # Normalise rating field in case model returns a string
            rating = parsed.get("rating")
            if isinstance(rating, str):
                low = rating.strip().lower()
                if low in ("satisfied", "positive", "good", "happy", "103"):
                    rating = 103
                elif low in ("not_satisfied", "negative", "bad", "unhappy", "-103"):
                    rating = -103
                elif rating.lstrip("-").isdigit():
                    rating = int(rating)
                else:
                    rating = None

            return {
                "feedback_received": bool(parsed.get("feedback_received")),
                "rating":            rating,
                "action":            parsed.get("action"),
            }

        except Exception as exc:
            log.warning("%s  ADK model %s failed for ticket %s: %s",
                        ICONS["warn"], model, ticket_id, exc)
            continue

    log.error("%s  All ADK model candidates failed for ticket %s", ICONS["error"], ticket_id)
    return None


# ---------------------------------------------------------------------------
# State management (dedup)
# ---------------------------------------------------------------------------

def load_state() -> set:
    if not STATE_FILE.exists():
        return set()
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        if not isinstance(data, list):
            log.warning("%s  State file malformed — resetting", ICONS["warn"])
            return set()
        return set(str(x) for x in data)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("%s  Could not load state file: %s — starting fresh", ICONS["warn"], exc)
        return set()


def save_state(state: set) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(sorted(state), f, indent=2)
    except OSError as exc:
        log.error("%s  Could not save state file: %s", ICONS["error"], exc)


# ---------------------------------------------------------------------------
# Ticket processing
# ---------------------------------------------------------------------------

def process_ticket(ticket: dict, state: set) -> bool:
    """
    Process one resolved ticket. Returns True if processed (state updated), False otherwise.
    """
    ticket_id = str(int(ticket.get("id", 0) or 0))
    if not ticket_id or ticket_id == "0":
        log.warning("%s  Ticket missing ID — skipping", ICONS["warn"])
        return False

    if ticket_id in state:
        log.debug("Ticket %s already processed — skipping", ticket_id)
        return False

    status = ticket.get("status")
    # Scalekit returns floats from Freshdesk JSON
    if status not in (4, 4.0):
        log.debug("Ticket %s status=%s (not resolved) — skipping", ticket_id, status)
        return False

    requester      = ticket.get("requester") or {}
    requester_email = requester.get("email") or ticket.get("requester_email")
    subject        = (ticket.get("subject") or "")[:60]

    log.info("%s  Ticket #%s | %s | %s", ICONS["ticket"], ticket_id, requester_email or "no-email", subject)

    # Fetch CSAT
    survey = fd_get_survey(ticket_id)
    if survey:
        rating_raw = survey.get("rating")
        log.info("%s  Survey found for ticket #%s — rating=%s", ICONS["survey"], ticket_id, rating_raw)
    else:
        log.info("%s  No survey result for ticket #%s — skipping", ICONS["skip"], ticket_id)
        # Mark as seen so we don't keep polling tickets with no survey
        state.add(ticket_id)
        save_state(state)
        return False

    # Ask ADK
    log.info("%s  Asking ADK for decision on ticket #%s...", ICONS["llm"], ticket_id)
    decision = get_adk_decision(ticket_id, survey, requester_email)

    if not decision:
        log.error("%s  ADK returned no decision for ticket #%s — skipping", ICONS["error"], ticket_id)
        return False

    if not decision.get("feedback_received"):
        log.info("%s  ADK says no feedback received for ticket #%s — skipping", ICONS["skip"], ticket_id)
        return False

    action = decision.get("action")
    rating = decision.get("rating")

    log.info("%s  Decision for ticket #%s: action=%s rating=%s",
             ICONS["action"], ticket_id, action, rating)

    # Execute action
    try:
        if action == "thank_and_close" or rating == 103:
            log.info("%s  Thanking and closing ticket #%s", ICONS["done"], ticket_id)
            fd_reply(ticket_id, _THANK_MSG)
            fd_update_status(ticket_id, 5)  # 5 = Closed
        elif action == "reopen_and_apologize" or rating == -103:
            log.info("%s  Reopening and apologizing for ticket #%s", ICONS["warn"], ticket_id)
            fd_reply(ticket_id, _REOPEN_MSG)
            fd_update_status(ticket_id, 2)  # 2 = Open
        else:
            log.warning("%s  Unrecognised action '%s' for ticket #%s — skipping",
                        ICONS["warn"], action, ticket_id)
            return False
    except RuntimeError as exc:
        log.error("%s  Freshdesk action failed for ticket #%s: %s",
                  ICONS["error"], ticket_id, exc)
        log.error("     The ticket was NOT marked processed so it will be retried next poll.")
        return False

    state.add(ticket_id)
    save_state(state)
    log.info("%s  Ticket #%s done and saved to state", ICONS["done"], ticket_id)
    return True


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

def _banner() -> None:
    tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    if not tty:
        print("Freshdesk CSAT Follow-up Agent starting...")
        return
    line = f"{CYAN}{BOLD}{'─' * 60}{RESET}"
    print(line)
    print(f"{CYAN}{BOLD}  Freshdesk CSAT Follow-up Agent{RESET}")
    print(f"{GREY}  Watches resolved tickets and acts on CSAT survey results{RESET}")
    print(line)
    print(f"  {GREY}Freshdesk identifier :{RESET} {WHITE}{FRESHDESK_IDENTIFIER}{RESET}")
    print(f"  {GREY}Scalekit env         :{RESET} {WHITE}{SCALEKIT_ENV_URL}{RESET}")
    print(f"  {GREY}Gemini model         :{RESET} {WHITE}{GEMINI_MODEL}{RESET}")
    print(f"  {GREY}Poll interval        :{RESET} {WHITE}{POLL_INTERVAL}s{RESET}")
    print(f"  {GREY}State file           :{RESET} {WHITE}{STATE_FILE}{RESET}")
    print(line)
    print()


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------

def poll() -> None:
    _banner()

    try:
        validate_config()
    except ValueError as exc:
        log.error("%s  Configuration error:\n  %s", ICONS["error"], exc)
        sys.exit(1)

    # Verify Scalekit + Freshdesk connection
    try:
        _get_sk()
        log.info("%s  Scalekit connected", ICONS["done"])
    except Exception as exc:
        log.error("%s  Cannot connect to Scalekit: %s", ICONS["error"], exc)
        sys.exit(1)

    # Pre-warm ADK agent at startup
    try:
        _get_adk_agent()
    except Exception as exc:
        log.error("%s  Cannot initialise ADK agent — check GOOGLE_ADK_API_KEY: %s",
                  ICONS["error"], exc)
        sys.exit(1)

    state = load_state()
    log.info("%s  Loaded %d already-processed ticket IDs from state", ICONS["start"], len(state))

    consecutive_errors = 0

    while True:
        log.info("%s  Polling Freshdesk for resolved tickets...", ICONS["poll"])
        try:
            tickets = fd_get_tickets()
            log.info("%s  Fetched %d resolved ticket(s)", ICONS["done"], len(tickets))
            consecutive_errors = 0

            processed = 0
            skipped   = 0
            for ticket in tickets:
                result = process_ticket(ticket, state)
                if result:
                    processed += 1
                else:
                    skipped += 1

            log.info("%s  Poll complete — processed=%d skipped=%d",
                     ICONS["done"], processed, skipped)

        except RuntimeError as exc:
            consecutive_errors += 1
            log.error("%s  Poll error (%d consecutive): %s",
                      ICONS["error"], consecutive_errors, exc)
            if consecutive_errors >= 5:
                log.error("%s  5 consecutive poll failures — check Scalekit credentials and network.",
                          ICONS["error"])

        log.info("%s  Next poll in %ds...", ICONS["poll"], POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    poll()
