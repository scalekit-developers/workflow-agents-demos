"""
Outbound Prospecting Agent: Apollo → Gmail → Google Sheets

Searches Apollo for ICP-matched prospects, drafts personalized Gmail outreach,
and logs everything to a Google Sheets tracker — so SDRs spend time selling,
not on admin.

Scalekit Agent Auth handles OAuth for all three connectors via execute_tool().
No manual token management.

LLM email drafting (OpenRouter) is used when OPENROUTER_API_KEY is set.
Falls back to a template-based drafter automatically if not set.

USE_SAMPLE_DATA=true  →  skip Apollo search, use bundled sample prospects.
                          Useful for a first run without an Apollo connector.

Setup:
  cp .env.example .env        # fill in your credentials
  pip install -r requirements.txt
  python run_flow.py
"""
import os, json, sys
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

CONNECTOR_USERS = {
    "apollo":       os.environ.get("APOLLO_USER", os.environ.get("GMAIL_USER", "")),
    "gmail":        os.environ["GMAIL_USER"],
    "googlesheets": os.environ["SHEETS_USER"],
}
SHEETS_ID       = os.environ["SHEETS_ID"]
SHEETS_RANGE    = os.environ.get("SHEETS_RANGE", "Sheet1!A:H")
USE_SAMPLE_DATA = os.environ.get("USE_SAMPLE_DATA", "false").lower() == "true"

ICP = {
    "titles":       os.environ.get("ICP_TITLES",    "VP of Sales,Head of Sales,Director of Sales,CRO").split(","),
    "industries":   os.environ.get("ICP_INDUSTRIES", "SaaS,Software,Technology").split(","),
    "employee_min": int(os.environ.get("ICP_EMP_MIN", "50")),
    "employee_max": int(os.environ.get("ICP_EMP_MAX", "5000")),
    "limit":        int(os.environ.get("PROSPECT_LIMIT", "5")),
}

# ── Error tracking ─────────────────────────────────────────────────────────────
_errors: list[str] = []

def log_error(step: str, detail: str) -> None:
    msg = f"[{step}] {detail}"
    _errors.append(msg)
    print(f"  ERROR {msg}")


# ── Sample prospects (used when USE_SAMPLE_DATA=true) ─────────────────────────
SAMPLE_PROSPECTS = [
    {
        "id": "sample_001",
        "first_name": "Sarah", "last_name": "Chen",
        "name": "Sarah Chen",
        "title": "VP of Sales",
        "email": "sarah.chen@novahq.io",
        "organization": {
            "name": "Nova HQ", "industry": "SaaS",
            "estimated_num_employees": 320, "funding_stage": "Series B",
            "short_description": "Nova HQ builds revenue intelligence tools for mid-market sales teams.",
        },
        "city": "San Francisco", "country": "United States",
        "linkedin_url": "https://linkedin.com/in/sarahchen",
        "buying_signals": ["Recent Series B ($28M)", "Hiring 4 AEs", "New VP Eng joined"],
    },
    {
        "id": "sample_002",
        "first_name": "Marcus", "last_name": "Webb",
        "name": "Marcus Webb",
        "title": "Head of Sales",
        "email": "mwebb@loopdata.com",
        "organization": {
            "name": "Loop Data", "industry": "Analytics",
            "estimated_num_employees": 180, "funding_stage": "Series A",
            "short_description": "Loop Data automates data pipeline monitoring for data engineering teams.",
        },
        "city": "Austin", "country": "United States",
        "linkedin_url": "https://linkedin.com/in/marcuswebb",
        "buying_signals": ["Using Salesforce + Outreach", "Grew headcount 40% YoY"],
    },
    {
        "id": "sample_003",
        "first_name": "Priya", "last_name": "Nair",
        "name": "Priya Nair",
        "title": "Director of Sales",
        "email": "pnair@stacklayer.dev",
        "organization": {
            "name": "StackLayer", "industry": "DevTools",
            "estimated_num_employees": 95, "funding_stage": "Seed",
            "short_description": "StackLayer simplifies cloud infrastructure provisioning for engineering teams.",
        },
        "city": "New York", "country": "United States",
        "linkedin_url": "https://linkedin.com/in/priyanair",
        "buying_signals": ["Just posted VP Sales role", "Product-led growth expanding to sales-led"],
    },
    {
        "id": "sample_004",
        "first_name": "James", "last_name": "Okafor",
        "name": "James Okafor",
        "title": "CRO",
        "email": "james@gridlineai.com",
        "organization": {
            "name": "Gridline AI", "industry": "AI/ML SaaS",
            "estimated_num_employees": 210, "funding_stage": "Series A",
            "short_description": "Gridline AI provides automated model monitoring and observability for ML teams.",
        },
        "city": "London", "country": "United Kingdom",
        "linkedin_url": "https://linkedin.com/in/jamesokafor",
        "buying_signals": ["Raised $12M 3 months ago", "Hiring SDRs and AEs aggressively"],
    },
    {
        "id": "sample_005",
        "first_name": "Elena", "last_name": "Rossi",
        "name": "Elena Rossi",
        "title": "VP of Sales",
        "email": "elena.rossi@forgecrm.io",
        "organization": {
            "name": "Forge CRM", "industry": "CRM Software",
            "estimated_num_employees": 430, "funding_stage": "Series B",
            "short_description": "Forge CRM is a vertical CRM built for construction and field service companies.",
        },
        "city": "Berlin", "country": "Germany",
        "linkedin_url": "https://linkedin.com/in/elenarossi",
        "buying_signals": ["Expanding into US market", "Recently replaced legacy outbound tooling"],
    },
]


# ── Connector availability tracking ──────────────────────────────────────────
_unavailable: set[str] = set()


# ── Auth helpers ──────────────────────────────────────────────────────────────
def ensure_authorized(connector: str) -> None:
    """Check connector status. Prints a magic link if not yet authorized.

    If the connector doesn't exist in Scalekit, marks it unavailable and
    continues — the pipeline will fall back for that step.
    """
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
            print(f"\n  [{connector}] Status={status}. Open to authorize:\n    {link}\n")
            input("  Press Enter after authorizing...")
            # Re-check after auth
            resp2 = connect.get_or_create_connected_account(
                connection_name=connector, identifier=identifier
            )
            if resp2.connected_account.status == "ACTIVE":
                print(f"  ✓ {connector} ({identifier}) — ACTIVE")
            else:
                log_error("auth", f"{connector} still not ACTIVE after auth (status={resp2.connected_account.status})")
                _unavailable.add(connector)
        else:
            print(f"  ✓ {connector} ({identifier}) — ACTIVE")
    except ScalekitNotFoundException:
        print(f"  ✗ {connector} — connector not found in Scalekit dashboard (skipping, will use fallback)")
        _unavailable.add(connector)
    except Exception as e:
        log_error("auth", f"{connector} check failed: {e.__class__.__name__}: {e}")
        _unavailable.add(connector)


def tool(connector: str, tool_name: str, **kwargs) -> dict:
    """Execute a Scalekit tool and return the data payload.

    Returns an empty dict if the connector is unavailable.
    Raises on unexpected errors so callers can decide how to handle them.
    """
    if connector in _unavailable:
        return {}
    try:
        result = connect.execute_tool(
            tool_name=tool_name,
            identifier=CONNECTOR_USERS[connector],
            tool_input=kwargs,
        )
        data = result.data or {}
        print(f"  (execute_tool:{tool_name} ✓)")
        return data
    except Exception as e:
        # Re-raise — callers decide whether to log, fallback, or abort
        raise RuntimeError(f"execute_tool({tool_name}) failed: {e.__class__.__name__}: {e}") from e


def _build_raw_mime(to: str, subject: str, body: str) -> str:
    """Encode an email as base64url MIME — required by the Gmail API."""
    import base64
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    msg = MIMEMultipart("alternative")
    msg["to"]      = to
    msg["subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def create_gmail_draft_via_scalekit(to: str, subject: str, body: str) -> dict:
    """Create a Gmail draft via Scalekit token + Gmail REST API.

    Scalekit owns the OAuth token via get_connected_account(), which auto-refreshes
    it before expiry. We pass the token to the Gmail drafts endpoint directly.
    Creates DRAFTS only — never sends.
    """
    import requests as http

    raw = _build_raw_mime(to, subject, body)
    identifier = CONNECTOR_USERS["gmail"]

    try:
        token_resp = connect.get_connected_account(
            connection_name="gmail", identifier=identifier
        )
        token = token_resp.connected_account.authorization_details["oauth_token"]["access_token"]
        resp = http.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": {"raw": raw}},
            timeout=30,
        )
        resp.raise_for_status()
        print("  (Scalekit token + Gmail REST ✓)")
        return resp.json()
    except Exception as e:
        raise RuntimeError(f"Gmail draft failed: {e.__class__.__name__}: {e}") from e


# ── ICP scoring ───────────────────────────────────────────────────────────────
def score_prospect(prospect: dict) -> int:
    """Score a prospect 0–100 against ICP criteria.

    Points breakdown:
      Title match       30 pts
      Industry match    25 pts
      Company size      20 pts
      Buying signals    25 pts (5 pts per signal, max 25)
    """
    score = 0
    title = (prospect.get("title") or "").lower()
    org   = prospect.get("organization") or {}

    if any(t.lower() in title for t in ICP["titles"]):
        score += 30

    industry = (org.get("industry") or "").lower()
    if any(i.lower() in industry for i in ICP["industries"]):
        score += 25

    emp = org.get("estimated_num_employees") or 0
    if ICP["employee_min"] <= emp <= ICP["employee_max"]:
        score += 20

    signals = prospect.get("buying_signals") or []
    score += min(len(signals) * 5, 25)

    return score


# ── Email drafting ─────────────────────────────────────────────────────────────
def _draft_with_llm(prospect: dict) -> tuple[str, str]:
    """Use OpenRouter to draft a personalized outreach email."""
    import requests as http

    org     = prospect.get("organization") or {}
    signals = "\n".join(f"- {s}" for s in (prospect.get("buying_signals") or []))

    prompt = f"""Write a short, personalized cold outreach email for a sales rep to send.

Prospect:
- Name: {prospect['name']}
- Title: {prospect.get('title', '')}
- Company: {org.get('name', '')}
- Industry: {org.get('industry', '')}
- Company description: {org.get('short_description', '')}
- Buying signals:
{signals}

Rules:
- Subject line: compelling, under 60 characters, no spam words
- Body: 3-4 short paragraphs max
- Reference ONE specific buying signal — make it feel researched, not templated
- End with a single low-friction CTA (15-min call, not a demo request)
- No generic openers ("Hope this finds you well")
- Tone: peer-to-peer, not vendor-to-prospect

Return ONLY valid JSON with keys: subject (string), body (string, use \\n for newlines)"""

    resp = http.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
        json={
            "model": os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
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
    parsed = json.loads(raw[start:end])
    return parsed["subject"], parsed["body"]


def _draft_template(prospect: dict) -> tuple[str, str]:
    """Template-based fallback — no LLM needed."""
    org     = prospect.get("organization") or {}
    name    = prospect.get("first_name") or prospect.get("name", "there")
    company = org.get("name", "your company")
    signals = prospect.get("buying_signals") or []
    signal  = signals[0] if signals else f"the growth at {company}"
    title   = prospect.get("title", "")

    subject = f"Quick question for {company}'s sales team"
    body = (
        f"Hi {name},\n\n"
        f"Noticed {signal} — congrats on the momentum.\n\n"
        f"We work with {title.lower()}s at similar-stage companies to cut the time reps spend on "
        f"admin (research, CRM updates, follow-up drafting) by around 70%. "
        f"The idea is to give back selling time, not add another tool to the stack.\n\n"
        f"Worth a 15-minute call to see if it's relevant for {company}?\n\n"
        f"Best"
    )
    return subject, body


def draft_email(prospect: dict) -> tuple[str, str]:
    """LLM draft if OPENROUTER_API_KEY is set; template fallback otherwise."""
    if os.environ.get("OPENROUTER_API_KEY"):
        try:
            subject, body = _draft_with_llm(prospect)
            print("  (LLM draft ✓)")
            return subject, body
        except Exception as e:
            print(f"  ⚠ LLM failed ({e.__class__.__name__}: {e}) — using template fallback")
    return _draft_template(prospect)


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 0 — Check connector auth
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── Step 0: Checking connector auth ──")
connectors_to_check = ["gmail", "googlesheets"]
if not USE_SAMPLE_DATA:
    connectors_to_check.insert(0, "apollo")
for connector in connectors_to_check:
    ensure_authorized(connector)

if USE_SAMPLE_DATA:
    print("  ℹ  USE_SAMPLE_DATA=true — skipping Apollo auth, using bundled prospects")

# Hard-stop if Gmail is unavailable — we can't draft anything without it
if "gmail" in _unavailable:
    log_error("auth", "Gmail connector unavailable — cannot draft emails. "
              "Set up the 'gmail' connection in Scalekit dashboard and re-run.")
    print("\n✗ Fatal: Gmail unavailable. See errors above.")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Find prospects (Apollo or sample data)
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── Step 1: Finding prospects ──")

if USE_SAMPLE_DATA:
    prospects = SAMPLE_PROSPECTS
    print(f"  Using {len(prospects)} sample prospects (set USE_SAMPLE_DATA=false to use Apollo)")
else:
    if "apollo" in _unavailable:
        log_error("step1", "Apollo connector unavailable — cannot search. "
                  "Set USE_SAMPLE_DATA=true or set up the apollo connector.")
        print("\n✗ Fatal: Apollo unavailable and USE_SAMPLE_DATA=false.")
        sys.exit(1)

    try:
        search_data = tool(
            "apollo", "apollo_search_contacts",
            titles=ICP["titles"],
            industries=ICP["industries"],
            employee_ranges=[f"{ICP['employee_min']},{ICP['employee_max']}"],
            limit=ICP["limit"] * 3,
        )
        raw_prospects = (
            search_data.get("contacts")
            or search_data.get("results")
            or search_data.get("people")
            or []
        )
        print(f"  Apollo returned {len(raw_prospects)} prospect(s)")
        if not raw_prospects:
            log_error("step1", "Apollo search returned 0 prospects — check ICP filter settings")
    except RuntimeError as e:
        log_error("step1", f"Apollo search failed: {e}")
        raw_prospects = []

    # Enrich each prospect — use person_id (not id) for the enrich call.
    # Only fill in fields that are missing from the search result; never
    # overwrite a non-empty field with an empty one from the enriched record.
    prospects = []
    for p in raw_prospects:
        # apollo_search_contacts returns both 'id' (CRM contact id) and
        # 'person_id' (prospecting person id). Enrich needs person_id.
        person_id = p.get("person_id") or p.get("id") or ""
        if person_id and not (p.get("email") and p.get("title") and p.get("name")):
            try:
                enriched = tool("apollo", "apollo_enrich_contact", id=person_id)
                enriched_obj = enriched.get("person") or enriched.get("contact") or {}
                # Safe merge: only fill in keys that are absent/empty in p
                for k, v in enriched_obj.items():
                    if v and not p.get(k):
                        p[k] = v
            except RuntimeError as e:
                log_error("step1", f"Enrich failed for {p.get('name', person_id)}: {e}")

        # Map organisation_name → organisation.name for scoring if needed
        if not p.get("organization") and p.get("organization_name"):
            p["organization"] = {"name": p["organization_name"]}
        # Pull employee count from account object if org is missing it
        if p.get("account") and p.get("organization") is not None:
            acc = p["account"]
            if not p["organization"].get("estimated_num_employees"):
                p["organization"]["estimated_num_employees"] = acc.get("estimated_num_employees") or 0
            if not p["organization"].get("industry"):
                p["organization"]["industry"] = acc.get("industry") or ""

        p.setdefault("buying_signals", [])
        if p.get("organization", {}).get("funding_stage"):
            p["buying_signals"].insert(0, f"Funding stage: {p['organization']['funding_stage']}")

        p["icp_score"] = score_prospect(p)
        prospects.append(p)

    prospects.sort(key=lambda x: x.get("icp_score", 0), reverse=True)
    prospects = prospects[:ICP["limit"]]
    print(f"  Filtered to top {len(prospects)} by ICP score")

# Score sample prospects
for p in prospects:
    if "icp_score" not in p:
        p["icp_score"] = score_prospect(p)
prospects.sort(key=lambda x: x.get("icp_score", 0), reverse=True)

if not prospects:
    log_error("step1", "No prospects to process — exiting")
    print("\n✗ Fatal: No prospects found.")
    sys.exit(1)

print(f"  Prospects ({len(prospects)}): {[p['name'] for p in prospects]}")


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2 — Draft personalized Gmail emails
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── Step 2: Drafting Gmail emails ──")
drafted = []
draft_errors = 0

for p in prospects:
    org   = p.get("organization") or {}
    email = p.get("email") or ""
    print(f"\n  [{p['name']} | {p.get('title','')} @ {org.get('name','')}]")
    print(f"  ICP score: {p.get('icp_score', 0)}/100")
    print(f"  Signals:   {', '.join(p.get('buying_signals') or []) or 'none'}")

    if not email:
        log_error("step2", f"No email for {p['name']} — skipping draft")
        continue

    subject, body = draft_email(p)

    try:
        draft_resp = create_gmail_draft_via_scalekit(to=email, subject=subject, body=body)
        draft_id   = draft_resp.get("id", "draft_created")
        draft_link = f"https://mail.google.com/mail/#drafts/{draft_id}"
        print(f"  Draft → {email}")
        print(f"  Subject: {subject}")
        print(f"  Link:    {draft_link}")
    except Exception as e:
        draft_id   = "error"
        draft_link = f"error"
        draft_errors += 1
        log_error("step2", f"Gmail draft failed for {p['name']} ({email}): {e}")

    drafted.append({
        **p,
        "email_subject": subject,
        "email_body":    body,
        "draft_id":      draft_id,
        "draft_link":    draft_link,
    })

print(f"\n  Drafted: {len(drafted) - draft_errors}/{len(prospects)} "
      f"({'all ok' if draft_errors == 0 else f'{draft_errors} error(s)'})")


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3 — Log to Google Sheets (fallback: local CSV)
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── Step 3: Logging prospects ──")

HEADERS = ["Name", "Company", "Title", "Email", "ICP Score",
           "Buying Signals", "Email Subject", "Draft Link"]

sheets_ok = "googlesheets" not in _unavailable and SHEETS_ID != "your_sheet_id_here"

if sheets_ok:
    import requests as http

    def _sheets_token() -> str:
        resp = connect.get_connected_account(
            connection_name="googlesheets",
            identifier=CONNECTOR_USERS["googlesheets"],
        )
        return resp.connected_account.authorization_details["oauth_token"]["access_token"]

    def _sheets_get(range_: str) -> dict:
        """Read a range — Tier 1: execute_tool, Tier 2: Sheets REST API."""
        try:
            data = tool("googlesheets", "googlesheets_get_values",
                        spreadsheet_id=SHEETS_ID, range=range_)
            return data
        except Exception as e1:
            print(f"  (googlesheets_get_values unavailable: {e1.__class__.__name__} — trying REST)")
        token = _sheets_token()
        resp = http.get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{SHEETS_ID}/values/{range_}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        resp.raise_for_status()
        print("  (Scalekit token + Sheets REST GET ✓)")
        return resp.json()

    def _sheets_append(values: list) -> None:
        """Append rows — Tier 1: execute_tool, Tier 2: Sheets REST API."""
        try:
            tool("googlesheets", "googlesheets_append_values",
                 spreadsheet_id=SHEETS_ID, range=SHEETS_RANGE, values=values)
            return
        except Exception as e1:
            print(f"  (googlesheets_append_values unavailable: {e1.__class__.__name__} — trying REST)")
        token = _sheets_token()
        resp = http.post(
            f"https://sheets.googleapis.com/v4/spreadsheets/{SHEETS_ID}"
            f"/values/{SHEETS_RANGE}:append"
            f"?valueInputOption=RAW&insertDataOption=INSERT_ROWS",
            headers={"Authorization": f"Bearer {token}"},
            json={"values": values},
            timeout=30,
        )
        resp.raise_for_status()
        print("  (Scalekit token + Sheets REST APPEND ✓)")

    # Write header row if sheet is empty
    try:
        header_check = _sheets_get(f"{SHEETS_RANGE.split('!')[0]}!A1:H1")
        if not (header_check.get("values") or []):
            _sheets_append([HEADERS])
            print("  Header row written ✓")
        else:
            print("  Header row already exists ✓")
    except Exception as e:
        log_error("step3", f"Header check failed: {e.__class__.__name__}: {e} — continuing without header")

    sheets_logged = 0
    for p in drafted:
        org     = p.get("organization") or {}
        signals = "; ".join(p.get("buying_signals") or [])
        try:
            _sheets_append([[
                p.get("name", ""),
                org.get("name", ""),
                p.get("title", ""),
                p.get("email", ""),
                p.get("icp_score", 0),
                signals,
                p.get("email_subject", ""),
                p.get("draft_link", ""),
            ]])
            sheets_logged += 1
            print(f"  ✓ {p['name']} @ {org.get('name', '')} → Sheets")
        except Exception as e:
            log_error("step3", f"Sheets append failed for {p['name']}: {e.__class__.__name__}: {e}")

    print(f"\n  Logged {sheets_logged}/{len(drafted)} rows to Google Sheets")
    print(f"  Sheet: https://docs.google.com/spreadsheets/d/{SHEETS_ID}")

else:
    # Fallback: local CSV
    import csv, pathlib
    csv_path = pathlib.Path(__file__).parent / "prospects_output.csv"
    write_header = not csv_path.exists()
    try:
        with open(csv_path, "a", newline="") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(HEADERS)
            for p in drafted:
                org     = p.get("organization") or {}
                signals = "; ".join(p.get("buying_signals") or [])
                writer.writerow([
                    p.get("name", ""),
                    org.get("name", ""),
                    p.get("title", ""),
                    p.get("email", ""),
                    p.get("icp_score", 0),
                    signals,
                    p.get("email_subject", ""),
                    p.get("draft_link", ""),
                ])
                print(f"  ✓ {p['name']} → {csv_path.name}")
        print(f"\n  Saved to: {csv_path}")
    except Exception as e:
        log_error("step3", f"CSV write failed: {e}")

    if "googlesheets" in _unavailable:
        print("  ℹ  Set up a 'googlesheets' connector in Scalekit to write to a real sheet")
    else:
        print("  ℹ  Set SHEETS_ID in .env to write to a real Google Sheet")


# ═══════════════════════════════════════════════════════════════════════════════
# FINAL SUMMARY
# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "═" * 60)
print("FLOW SUMMARY")
print("═" * 60)
print(f"  Prospects found:    {len(prospects)}")
good_drafts = sum(1 for p in drafted if p.get("draft_id") not in ("error", "local_only"))
print(f"  Gmail drafts:       {good_drafts}/{len(drafted)} created"
      f"{' ✓' if good_drafts == len(drafted) else f'  ← {draft_errors} failed'}")
if sheets_ok:
    print(f"  Sheets logged:      {sheets_logged}/{len(drafted)} rows"
          f"{' ✓' if sheets_logged == len(drafted) else f'  ← {len(drafted)-sheets_logged} failed'}")
else:
    print(f"  Sheets:             CSV fallback (googlesheets not configured)")

if _errors:
    print(f"\n  ⚠ {len(_errors)} error(s) during this run:")
    for err in _errors:
        print(f"    • {err}")
else:
    print("\n  ✓ No errors")

if good_drafts > 0:
    print(f"\n  Drafts inbox: https://mail.google.com/mail/#drafts")
if sheets_ok:
    print(f"  Sheet:        https://docs.google.com/spreadsheets/d/{SHEETS_ID}")
print("═" * 60)
