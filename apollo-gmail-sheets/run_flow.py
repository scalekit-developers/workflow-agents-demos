"""
Outbound Prospecting Agent: Apollo -> Gmail -> Google Sheets

Searches Apollo for ICP-matched prospects, drafts personalized Gmail outreach,
and logs everything to a Google Sheets tracker.

Scalekit Agent Auth handles OAuth for Apollo, Gmail, and Google Sheets via
execute_tool(). No token management needed in application code.

LLM email drafting uses OpenRouter when OPENROUTER_API_KEY is set.
Falls back to a template that references real buying signals from Apollo.

Setup:
  cp .env.example .env        # fill in your credentials and connector names
  pip install -r requirements.txt
  python run_flow.py
"""
import os
import sys
import json
import logging
import csv
import pathlib

# Force gRPC to use the native DNS resolver — the default async resolver can
# fail DNS lookups in some macOS / VPN / sandbox environments.
os.environ.setdefault("GRPC_DNS_RESOLVER", "native")

import settings

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RED    = "\033[91m"
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

ICONS = {
    "start":  "▶",
    "done":   "✔",
    "skip":   "–",
    "warn":   "⚠",
    "error":  "✖",
    "auth":   "⚙",
    "search": "⌕",
    "draft":  "✉",
    "sheet":  "▦",
    "llm":    "✦",
}


class _NoiseFilter(logging.Filter):
    _SKIP = ("AFC is enabled",)

    def filter(self, record: logging.LogRecord) -> bool:
        return not any(s in record.getMessage() for s in self._SKIP)


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


def _setup_logging() -> None:
    is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_ColorFormatter(colorize=is_tty))
    handler.addFilter(_NoiseFilter())
    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.LOG_LEVEL, logging.INFO))
    root.handlers = [handler]
    for noisy in ("httpx", "httpcore", "grpc", "urllib3",
                  "google.auth", "google.genai", "google.generativeai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_setup_logging()
log = logging.getLogger("prospecting-agent")

# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

try:
    settings.validate()
except ValueError as exc:
    tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    msg = f"\n{RED}{BOLD}Configuration error:{RESET}\n  {exc}\n" if tty else f"\nConfiguration error:\n  {exc}\n"
    print(msg)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Scalekit client
# ---------------------------------------------------------------------------

from scalekit import ScalekitClient

sk = ScalekitClient(
    env_url=settings.SCALEKIT_ENV_URL,
    client_id=settings.SCALEKIT_CLIENT_ID,
    client_secret=settings.SCALEKIT_CLIENT_SECRET,
)

# Map each logical connector to (user identifier, exact connection name)
CONNECTORS: dict[str, tuple[str, str]] = {
    "apollo":       (settings.APOLLO_USER,  settings.APOLLO_CONNECTION_NAME),
    "gmail":        (settings.GMAIL_USER,   settings.GMAIL_CONNECTION_NAME),
    "googlesheets": (settings.SHEETS_USER,  settings.SHEETS_CONNECTION_NAME),
}

_unavailable: set[str] = set()
_run_errors:  list[str] = []


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

def _banner() -> None:
    tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    if not tty:
        print("Outbound Prospecting Agent starting...")
        return
    line = f"{CYAN}{BOLD}{'─' * 60}{RESET}"
    print(line)
    print(f"{CYAN}{BOLD}  Outbound Prospecting Agent{RESET}")
    print(f"{GREY}  Apollo → Gmail Drafts → Google Sheets via Scalekit{RESET}")
    print(line)
    print(f"  {GREY}Environment  :{RESET} {WHITE}{settings.SCALEKIT_ENV_URL}{RESET}")
    print(f"  {GREY}Apollo       :{RESET} {WHITE}{settings.APOLLO_USER} / {settings.APOLLO_CONNECTION_NAME}{RESET}")
    print(f"  {GREY}Gmail        :{RESET} {WHITE}{settings.GMAIL_USER} / {settings.GMAIL_CONNECTION_NAME}{RESET}")
    print(f"  {GREY}Sheets       :{RESET} {WHITE}{settings.SHEETS_USER} / {settings.SHEETS_CONNECTION_NAME}{RESET}")
    print(f"  {GREY}LLM drafting :{RESET} {WHITE}{'OpenRouter (' + settings.OPENROUTER_MODEL + ')' if settings.OPENROUTER_API_KEY else 'template fallback'}{RESET}")
    print(f"  {GREY}Prospect cap :{RESET} {WHITE}{settings.PROSPECT_LIMIT}{RESET}")
    print(line)
    print()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def ensure_authorized(connector: str) -> None:
    """Check connector status. If not ACTIVE, print auth link and wait. Marks
    unavailable on permanent failure so callers can decide whether to abort."""
    from scalekit.common.exceptions import ScalekitNotFoundException

    identifier, connection_name = CONNECTORS[connector]
    log.info("%s  Checking %s (user=%s, connector=%s)...",
             ICONS["auth"], connector, identifier, connection_name)
    try:
        resp   = sk.actions.get_or_create_connected_account(
            connection_name=connection_name, identifier=identifier
        )
        status = resp.connected_account.status
        if status == "ACTIVE":
            log.info("%s  %s — ACTIVE", ICONS["done"], connector)
            return

        # Not active — print auth link and wait for user
        link = sk.actions.get_authorization_link(
            connection_name=connection_name, identifier=identifier
        ).link
        log.warning("%s  %s status=%s — open this link to authorize:\n    %s",
                    ICONS["warn"], connector, status, link)
        input(f"  Press Enter after completing OAuth for {connector}...")

        resp2  = sk.actions.get_or_create_connected_account(
            connection_name=connection_name, identifier=identifier
        )
        status2 = resp2.connected_account.status
        if status2 == "ACTIVE":
            log.info("%s  %s — ACTIVE", ICONS["done"], connector)
        else:
            log.error("%s  %s still not ACTIVE after auth (status=%s) — skipping this connector",
                      ICONS["error"], connector, status2)
            _unavailable.add(connector)

    except ScalekitNotFoundException:
        log.error(
            "%s  %s — connector '%s' not found in Scalekit.\n"
            "       Go to app.scalekit.com → Agent Auth → Connections and add it,\n"
            "       then set %s_CONNECTION_NAME in .env to the exact connector name.",
            ICONS["error"], connector, connection_name, connector.upper()
        )
        _unavailable.add(connector)

    except Exception as exc:
        raw = str(exc)
        short = next((l.strip() for l in raw.splitlines() if l.strip()), repr(exc))[:120]
        log.error("%s  %s auth check failed: %s", ICONS["error"], connector, short)
        _unavailable.add(connector)


def execute_tool(connector: str, tool_name: str, **kwargs) -> dict:
    """Call a Scalekit action tool. Raises RuntimeError on any failure — never swallows."""
    if connector in _unavailable:
        raise RuntimeError(f"{connector} connector is unavailable (auth failed or not found)")
    identifier, connection_name = CONNECTORS[connector]
    log.debug("execute_tool  tool=%s  connector=%s  identifier=%s  input=%s",
              tool_name, connection_name, identifier, kwargs)
    try:
        result = sk.actions.execute_tool(
            tool_name=tool_name,
            connection_name=connection_name,
            identifier=identifier,
            tool_input=kwargs,
        )
        data = result.data or {}
        log.debug("execute_tool  tool=%s  response_keys=%s", tool_name, list(data.keys()))
        return data
    except Exception as exc:
        import traceback as _tb
        log.debug("execute_tool exception for %s:\n%s", tool_name, _tb.format_exc())
        raw = str(exc)
        # Scalekit exceptions often start with newlines; strip and find the useful part
        # Try gRPC details field first (most specific)
        for marker in ('details = "', 'tool_error_message:', 'Error Code:'):
            idx = raw.find(marker)
            if idx != -1:
                snippet = raw[idx:idx + 300].split("\n")[0].strip().strip('"')
                if snippet:
                    raise RuntimeError(f"{tool_name} failed: {snippet}") from exc
        # Fall back to first non-empty line
        first = next((l.strip() for l in raw.splitlines() if l.strip()), repr(exc)[:160])
        raise RuntimeError(f"{tool_name} failed: {first[:160]}") from exc


# ---------------------------------------------------------------------------
# ICP scoring
# ---------------------------------------------------------------------------

def _score(prospect: dict) -> int:
    score = 0
    title = (prospect.get("title") or "").lower()
    org   = prospect.get("organization") or {}

    if any(t.lower() in title for t in settings.ICP_TITLES):
        score += 30

    industry = (org.get("industry") or "").lower()
    if any(i.lower() in industry for i in settings.ICP_INDUSTRIES):
        score += 25

    emp = int(org.get("estimated_num_employees") or 0)
    if settings.ICP_EMP_MIN <= emp <= settings.ICP_EMP_MAX:
        score += 20

    score += min(len(prospect.get("buying_signals") or []) * 5, 25)
    return score


# ---------------------------------------------------------------------------
# Apollo: search + enrich
# ---------------------------------------------------------------------------

def fetch_prospects() -> list[dict]:
    """Call Apollo search, enrich each result, extract buying signals, score."""
    title_str    = ",".join(settings.ICP_TITLES)
    industry_str = ",".join(settings.ICP_INDUSTRIES)
    per_page     = min(settings.PROSPECT_LIMIT * 3, 100)

    log.info("%s  Calling apollo_search_contacts (titles=%s, industries=%s, per_page=%d)",
             ICONS["search"], settings.ICP_TITLES, settings.ICP_INDUSTRIES, per_page)
    try:
        # Apollo's backend expects arrays for title and industry even though
        # the tool schema declares them as strings.
        data = execute_tool(
            "apollo", "apollo_search_contacts",
            title=settings.ICP_TITLES,
            industry=settings.ICP_INDUSTRIES,
            per_page=per_page,
        )
    except RuntimeError as exc:
        log.error("%s  Apollo search failed: %s", ICONS["error"], exc)
        raise

    raw = data.get("contacts") or data.get("people") or data.get("results") or []
    log.info("%s  Apollo returned %d raw prospect(s) (response keys: %s)",
             ICONS["done"], len(raw), list(data.keys()))

    if not raw:
        log.warning(
            "%s  Apollo search returned 0 contacts (total_entries=%s).\n"
            "       Possible causes:\n"
            "         1. Apollo free tier has limited prospecting credits — check apollo.io → Credits\n"
            "         2. ICP filters are too narrow — try broadening ICP_TITLES or ICP_INDUSTRIES in .env\n"
            "         3. Apollo connector needs the 'Sequences & Email' plan for people search",
            ICONS["warn"],
            data.get("pagination", {}).get("total_entries", "?")
        )

    prospects = []
    for p in raw:
        person_id = p.get("person_id") or p.get("id") or ""

        # Enrich if missing key fields
        if person_id and not (p.get("email") and p.get("title") and p.get("name")):
            log.debug("Enriching prospect id=%s name=%s", person_id, p.get("name", "?"))
            try:
                enriched = execute_tool("apollo", "apollo_enrich_contact", id=person_id)
                obj = enriched.get("person") or enriched.get("contact") or {}
                for k, v in obj.items():
                    if v and not p.get(k):
                        p[k] = v
                log.debug("Enriched %s — got email=%s title=%s",
                          p.get("name", person_id), bool(p.get("email")), bool(p.get("title")))
            except RuntimeError as exc:
                log.warning("%s  Enrich failed for %s: %s",
                            ICONS["warn"], p.get("name", person_id), exc)

        # Normalise org field — Apollo sometimes uses account or organization_name
        if not p.get("organization"):
            if p.get("organization_name"):
                p["organization"] = {"name": p["organization_name"]}
            elif p.get("account"):
                p["organization"] = p["account"]

        org = p.get("organization") or {}
        if p.get("account"):
            acc = p["account"]
            if not org.get("estimated_num_employees"):
                org["estimated_num_employees"] = acc.get("estimated_num_employees") or 0
            if not org.get("industry"):
                org["industry"] = acc.get("industry") or ""
            if not org.get("name"):
                org["name"] = acc.get("name") or ""
            p["organization"] = org

        # Extract buying signals from Apollo fields
        signals = list(p.get("buying_signals") or [])
        fs = org.get("funding_stage")
        if fs and f"Funding stage: {fs}" not in signals:
            signals.insert(0, f"Funding stage: {fs}")
        keywords = p.get("keywords") or []
        for kw in keywords[:3]:
            if kw not in signals:
                signals.append(kw)
        p["buying_signals"] = signals

        p["icp_score"] = _score(p)
        log.debug("Prospect %s | title=%s | org=%s | score=%d",
                  p.get("name"), p.get("title"), org.get("name"), p["icp_score"])
        prospects.append(p)

    prospects.sort(key=lambda x: x["icp_score"], reverse=True)
    top = prospects[:settings.PROSPECT_LIMIT]

    log.info("%s  Top %d/%d prospects after ICP scoring:",
             ICONS["done"], len(top), len(prospects))
    for p in top:
        log.info("     %-30s %-25s score=%d",
                 p.get("name", "?"), p.get("title", "")[:25], p["icp_score"])

    return top


# ---------------------------------------------------------------------------
# Email drafting
# ---------------------------------------------------------------------------

def _draft_llm(prospect: dict) -> tuple[str, str]:
    import requests as http
    org     = prospect.get("organization") or {}
    signals = "\n".join(f"- {s}" for s in (prospect.get("buying_signals") or []))
    prompt  = (
        f"Write a short cold outreach email.\n\n"
        f"Prospect: {prospect.get('name','')}, {prospect.get('title','')} at {org.get('name','')}\n"
        f"Industry: {org.get('industry','')}\n"
        f"About company: {org.get('short_description') or org.get('name','')}\n"
        f"Buying signals:\n{signals or '(none available)'}\n\n"
        f"Rules:\n"
        f"- Subject line under 60 chars, no spam words\n"
        f"- 3-4 short paragraphs, peer-to-peer tone\n"
        f"- Reference ONE specific buying signal\n"
        f"- CTA: 15-min call, not a demo\n"
        f"- No generic openers like 'I hope this email finds you'\n\n"
        f"Return ONLY valid JSON with two keys: {{\"subject\": \"...\", \"body\": \"...\"}}"
    )
    log.debug("Calling OpenRouter model=%s", settings.OPENROUTER_MODEL)
    resp = http.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"},
        json={
            "model": settings.OPENROUTER_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
        },
        timeout=30,
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"].strip()
    log.debug("LLM raw response (first 200): %s", raw[:200])

    # Strip markdown fences if the model wrapped it
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    start, end = raw.find("{"), raw.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError(f"LLM response did not contain JSON: {raw[:200]}")
    parsed = json.loads(raw[start:end])
    if "subject" not in parsed or "body" not in parsed:
        raise ValueError(f"LLM JSON missing subject/body keys: {parsed}")
    return parsed["subject"], parsed["body"]


def _draft_template(prospect: dict) -> tuple[str, str]:
    org     = prospect.get("organization") or {}
    name    = prospect.get("first_name") or (prospect.get("name") or "there").split()[0]
    company = org.get("name") or "your company"
    title   = prospect.get("title") or "sales leader"
    signals = prospect.get("buying_signals") or []
    signal  = signals[0] if signals else f"the growth at {company}"
    # Use plain ASCII apostrophe and hyphen — Scalekit's template engine chokes
    # on Unicode dashes (em-dash U+2014) inside tool_input strings.
    subject = f"Quick question for {company}"
    body = "\n\n".join([
        f"Hi {name},",
        f"Noticed {signal} - congrats on the momentum.",
        (
            f"We work with {title.lower()}s at similar-stage companies to cut the time reps spend"
            f" on admin (research, CRM updates, follow-up drafting) by around 70%."
            f" The idea is to give back selling time, not add another tool to the stack."
        ),
        f"Worth a 15-minute call to see if it is relevant for {company}?",
        "Best",
    ])
    return subject, body


def draft_email(prospect: dict) -> tuple[str, str]:
    if settings.OPENROUTER_API_KEY:
        try:
            subject, body = _draft_llm(prospect)
            log.info("%s  LLM draft OK for %s", ICONS["llm"], prospect.get("name"))
            return subject, body
        except Exception as exc:
            short = str(exc).split("\n")[0][:120]
            log.warning("%s  LLM failed for %s (%s) — using template fallback",
                        ICONS["warn"], prospect.get("name"), short)
    subject, body = _draft_template(prospect)
    log.debug("Template draft for %s: subject=%s", prospect.get("name"), subject)
    return subject, body


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

def create_gmail_draft(to: str, subject: str, body: str) -> dict:
    log.debug("Creating Gmail draft to=%s subject=%s", to, subject)
    data = execute_tool(
        "gmail", "gmail_create_draft",
        to=to,
        subject=subject,
        body=body,
        content_type="text/plain",
    )
    log.debug("Gmail draft created: %s", data)
    return data


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

SHEETS_HEADERS = ["Name", "Company", "Title", "Email", "ICP Score",
                  "Buying Signals", "Email Subject", "Draft Link"]


def _sheets_get_values(sheet_id: str, range_: str) -> list:
    data = execute_tool(
        "googlesheets", "googlesheets_get_values",
        spreadsheet_id=sheet_id,
        range=range_,
    )
    return data.get("values") or []


def _sheets_append(sheet_id: str, rows: list) -> None:
    data = execute_tool(
        "googlesheets", "googlesheets_append_values",
        spreadsheet_id=sheet_id,
        range=settings.SHEETS_RANGE,
        values=rows,
        value_input_option="RAW",
        insert_data_option="INSERT_ROWS",
    )
    log.debug("Sheets appended rows, response keys: %s", list(data.keys()))


# ===========================================================================
# MAIN PIPELINE
# ===========================================================================

def main() -> None:
    _banner()

    # -----------------------------------------------------------------------
    # Step 0: Auth — all three connectors must be checked
    # -----------------------------------------------------------------------
    log.info("%s  Step 0: Connector auth", ICONS["start"])
    for connector in ["apollo", "gmail", "googlesheets"]:
        ensure_authorized(connector)

    # Gmail and Apollo are hard requirements
    missing_critical = [c for c in ("apollo", "gmail") if c in _unavailable]
    if missing_critical:
        for c in missing_critical:
            log.error(
                "%s  %s is unavailable — cannot continue.\n"
                "       Add connector '%s' in Scalekit → Agent Auth → Connections,\n"
                "       authorize it, then set %s_CONNECTION_NAME in .env.",
                ICONS["error"], c, CONNECTORS[c][1], c.upper()
            )
        sys.exit(1)

    sheets_connector_ok = "googlesheets" not in _unavailable
    sheets_id = settings.SHEETS_ID if (settings.SHEETS_ID and settings.SHEETS_ID != "your_sheet_id_here") else ""

    # Auto-create a sheet via Scalekit if the connector is active but no SHEETS_ID is set
    if sheets_connector_ok and not sheets_id:
        log.info("%s  SHEETS_ID not set — creating a new Google Sheet via Scalekit...", ICONS["sheet"])
        try:
            data = execute_tool(
                "googlesheets", "googlesheets_create_spreadsheet",
                title="Prospecting Agent - Outreach Tracker",
            )
            sheets_id = data.get("spreadsheetId", "")
            sheet_url = data.get("spreadsheetUrl", "")
            if sheets_id:
                log.info("%s  Sheet created: %s", ICONS["done"], sheet_url)
                log.info("     Add this to .env to reuse on next run: SHEETS_ID=%s", sheets_id)
            else:
                log.warning("%s  Sheet creation returned no ID — falling back to CSV", ICONS["warn"])
        except RuntimeError as exc:
            log.warning("%s  Could not create sheet: %s — falling back to CSV", ICONS["warn"], exc)

    sheets_available = sheets_connector_ok and bool(sheets_id)

    if not sheets_available:
        if not sheets_connector_ok:
            log.warning(
                "%s  Google Sheets connector unavailable — results will be saved to CSV.\n"
                "       Add 'googlesheets' connector in Scalekit → Agent Auth → Connections.",
                ICONS["warn"]
            )
        else:
            log.warning("%s  Could not resolve a Sheet ID — results will be saved to CSV.", ICONS["warn"])

    # -----------------------------------------------------------------------
    # Step 1: Apollo — search and enrich
    # -----------------------------------------------------------------------
    log.info("%s  Step 1: Fetching prospects from Apollo", ICONS["search"])
    try:
        prospects = fetch_prospects()
    except RuntimeError as exc:
        log.error("%s  Could not fetch prospects: %s", ICONS["error"], exc)
        sys.exit(1)

    if not prospects:
        log.error(
            "%s  No prospects returned from Apollo after scoring.\n"
            "       Broaden ICP_TITLES, ICP_INDUSTRIES, or ICP_EMP range in .env.",
            ICONS["error"]
        )
        sys.exit(1)

    # -----------------------------------------------------------------------
    # Step 2: Draft emails and create Gmail drafts
    # -----------------------------------------------------------------------
    log.info("%s  Step 2: Drafting emails and creating Gmail drafts (%d prospects)",
             ICONS["draft"], len(prospects))
    drafted      = []   # all attempted (success + failed drafts)
    draft_ok     = 0   # successfully created Gmail drafts
    draft_errors = 0   # failed Gmail draft creations
    skipped      = 0   # no email address

    for p in prospects:
        org   = p.get("organization") or {}
        email = p.get("email") or ""
        name  = p.get("name") or "Unknown"

        log.info("     %s | %s @ %s | ICP score=%d",
                 name, p.get("title", ""), org.get("name", ""), p.get("icp_score", 0))
        if p.get("buying_signals"):
            log.info("     Signals: %s", ", ".join(p["buying_signals"]))

        if not email:
            msg = f"No email address for {name} — skipping draft"
            log.warning("%s  %s", ICONS["warn"], msg)
            _run_errors.append(msg)
            skipped += 1
            continue

        subject, body = draft_email(p)

        draft_id  = ""
        draft_url = ""
        try:
            resp      = create_gmail_draft(to=email, subject=subject, body=body)
            draft_id  = resp.get("id", "")
            draft_url = f"https://mail.google.com/mail/#drafts/{draft_id}" if draft_id else "created"
            draft_ok += 1
            log.info("%s  Draft created → %s", ICONS["done"], email)
            log.info("     Subject: %s", subject)
            log.info("     Link   : %s", draft_url)
        except RuntimeError as exc:
            draft_id  = "error"
            draft_url = "error"
            draft_errors += 1
            log.error("%s  Gmail draft failed for %s: %s", ICONS["error"], name, exc)
            _run_errors.append(f"Gmail draft failed for {name}: {exc}")

        drafted.append({
            **p,
            "email_subject": subject,
            "email_body":    body,
            "draft_id":      draft_id,
            "draft_link":    draft_url,
        })

    attempted = len(prospects) - skipped
    if skipped:
        log.warning("%s  %d prospect(s) had no email — skipped", ICONS["warn"], skipped)
    if draft_errors:
        log.warning("%s  Drafted %d/%d (%d failed)",
                    ICONS["warn"], draft_ok, attempted, draft_errors)
    else:
        log.info("%s  Drafted %d/%d", ICONS["done"], draft_ok, attempted)

    # -----------------------------------------------------------------------
    # Step 3: Log to Google Sheets (CSV fallback)
    # -----------------------------------------------------------------------
    log.info("%s  Step 3: Logging results", ICONS["sheet"])
    logged = 0

    if sheets_available:
        # Write header row if the sheet is empty
        try:
            existing = _sheets_get_values(
                sheets_id, f"{settings.SHEETS_RANGE.split('!')[0]}!A1:H1"
            )
            if not existing:
                _sheets_append(sheets_id, [SHEETS_HEADERS])
                log.info("%s  Header row written to sheet", ICONS["done"])
        except RuntimeError as exc:
            log.warning("%s  Could not check/write sheet header: %s", ICONS["warn"], exc)

        for p in drafted:
            if p.get("draft_id") == "error":
                log.debug("Skipping Sheets row for %s — draft failed", p.get("name"))
                continue
            org     = p.get("organization") or {}
            signals = "; ".join(p.get("buying_signals") or [])
            row     = [
                p.get("name", ""),
                org.get("name", ""),
                p.get("title", ""),
                p.get("email", ""),
                p.get("icp_score", 0),
                signals,
                p.get("email_subject", ""),
                p.get("draft_link", ""),
            ]
            try:
                _sheets_append(sheets_id, [row])
                logged += 1
                log.info("%s  %s @ %s → Sheets", ICONS["done"], p.get("name"), org.get("name", ""))
            except RuntimeError as exc:
                log.error("%s  Sheets append failed for %s: %s",
                          ICONS["error"], p.get("name"), exc)
                _run_errors.append(f"Sheets failed for {p.get('name')}: {exc}")

        log.info("%s  Logged %d/%d rows to Sheets", ICONS["done"], logged, len(drafted))
        log.info("     https://docs.google.com/spreadsheets/d/%s", sheets_id)

    else:
        csv_path   = pathlib.Path(__file__).parent / "prospects_output.csv"
        write_hdr  = not csv_path.exists()
        try:
            with open(csv_path, "a", newline="") as f:
                writer = csv.writer(f)
                if write_hdr:
                    writer.writerow(SHEETS_HEADERS)
                for p in drafted:
                    org     = p.get("organization") or {}
                    signals = "; ".join(p.get("buying_signals") or [])
                    writer.writerow([
                        p.get("name", ""), org.get("name", ""), p.get("title", ""),
                        p.get("email", ""), p.get("icp_score", 0), signals,
                        p.get("email_subject", ""), p.get("draft_link", ""),
                    ])
                    logged += 1
            log.info("%s  Saved %d row(s) to %s", ICONS["done"], logged, csv_path)
        except OSError as exc:
            log.error("%s  CSV write failed: %s", ICONS["error"], exc)
            _run_errors.append(f"CSV write failed: {exc}")

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    sep = f"{CYAN}{'─' * 60}{RESET}" if tty else "─" * 60
    print(f"\n{sep}")
    log.info("%s  Run complete", ICONS["done"])
    log.info("     Prospects found : %d", len(prospects))
    if skipped:
        log.info("     No-email skipped: %d", skipped)
    log.info("     Gmail drafts    : %d/%d", draft_ok, attempted)
    if sheets_available:
        log.info("     Sheets rows     : %d/%d", logged, draft_ok)
    else:
        log.info("     Output file     : prospects_output.csv")
    if _run_errors:
        log.warning("%s  %d error(s) this run:", ICONS["warn"], len(_run_errors))
        for e in _run_errors:
            log.warning("     %s", e)
    else:
        log.info("%s  No errors", ICONS["done"])
    if draft_ok > 0:
        log.info("     Drafts inbox    : https://mail.google.com/mail/#drafts")
    print(sep)


if __name__ == "__main__":
    main()
