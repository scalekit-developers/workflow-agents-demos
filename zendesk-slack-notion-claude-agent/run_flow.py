"""
Support Triage Agent: Zendesk + Slack + Notion

Polls Zendesk for new/unprocessed tickets, classifies them by category and severity
using an LLM, searches a Notion knowledge base for matching articles, routes to the
appropriate Slack channel, and updates the Zendesk ticket with tags and internal notes.

Scalekit Agent Auth handles auth for all three connectors. Token storage, refresh,
and API calls all go through actions.execute_tool(). No manual token management.

Setup:
  cp .env.example .env        # fill in your credentials
  pip install scalekit-sdk-python requests python-dotenv
  python run_flow.py
"""

import os, sys, json, time
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
import scalekit.client

load_dotenv()

# ── Scalekit client ───────────────────────────────────────────────────────────
sk = scalekit.client.ScalekitClient(
    client_id=os.environ["SCALEKIT_CLIENT_ID"],
    client_secret=os.environ["SCALEKIT_CLIENT_SECRET"],
    env_url=os.environ["SCALEKIT_ENV_URL"],
)
actions = sk.actions

# ── Connector configuration (all from .env) ──────────────────────────────────
SLACK_CONNECTOR = os.environ.get("SLACK_CONNECTOR", "slack")
CONNECTOR_USERS = {
    "zendesk":       os.environ["ZENDESK_USER"],
    SLACK_CONNECTOR: os.environ["SLACK_USER"],
    "notion":        os.environ["NOTION_USER"],
}
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", CONNECTOR_USERS["zendesk"])
NOTION_DB_ID = os.environ.get("NOTION_DB_ID", "")

POLLING_MODE = os.environ.get("POLLING_MODE", "false").lower() == "true"
POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL_MINUTES", "2"))

# ── Channel routing (configurable via env, with sensible defaults) ────────────
# Set these in .env to map each category to a Slack channel name or ID.
# Example: CHANNEL_BUG=#engineering  or  CHANNEL_BUG=C0AKYEQ11L6
FALLBACK_CHANNEL = os.environ.get("FALLBACK_CHANNEL", "#support-triage")
CHANNEL_MAP = {
    "bug":             os.environ.get("CHANNEL_BUG", "#engineering"),
    "billing":         os.environ.get("CHANNEL_BILLING", "#billing"),
    "feature_request": os.environ.get("CHANNEL_FEATURE", "#product-feedback"),
    "how_to":          os.environ.get("CHANNEL_HOWTO", "#support-triage"),
    "account_issue":   os.environ.get("CHANNEL_ACCOUNT", "#support-triage"),
}

SEVERITY_PRIORITY = {
    "P0": "urgent",
    "P1": "high",
    "P2": "normal",
    "P3": "low",
}

# ── De-duplication: track processed ticket IDs across polling cycles ──────────
STATE_FILE = Path(__file__).parent / "state" / "processed_tickets.json"
MAX_PROCESSED_IDS = 5000  # cap to prevent unbounded file growth
_processed_ids: set[str] = set()


def _load_processed_ids() -> None:
    global _processed_ids
    if STATE_FILE.exists():
        try:
            _processed_ids = set(json.loads(STATE_FILE.read_text()))
        except (json.JSONDecodeError, TypeError):
            _processed_ids = set()


def _save_processed_ids() -> None:
    """Atomic write: write to a temp file first, then rename. Prevents corruption
    if the process crashes mid-write or two polling cycles overlap.
    Also caps the set at MAX_PROCESSED_IDS to prevent unbounded growth --
    oldest IDs (lowest numerically) are evicted first."""
    global _processed_ids
    if len(_processed_ids) > MAX_PROCESSED_IDS:
        # Keep only the most recent IDs (highest ticket numbers)
        _processed_ids = set(sorted(_processed_ids, key=lambda x: int(x) if x.isdigit() else 0)[-MAX_PROCESSED_IDS:])
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(_processed_ids)))
    tmp.replace(STATE_FILE)  # atomic on POSIX; near-atomic on Windows


def _mark_processed(ticket_id: str) -> None:
    _processed_ids.add(str(ticket_id))
    _save_processed_ids()


def _is_processed(ticket_id: str) -> bool:
    return str(ticket_id) in _processed_ids


# ── Auth helpers ──────────────────────────────────────────────────────────────
def ensure_authorized(connector: str) -> None:
    """Check connector status. Prints an auth link if not yet authorized."""
    identifier = CONNECTOR_USERS[connector]
    resp = actions.get_or_create_connected_account(
        connection_name=connector, identifier=identifier
    )
    status = resp.connected_account.status
    if status != "ACTIVE":
        print(f"\n  ⚠ {connector} ({identifier}) -- {status}")
        try:
            link = actions.get_authorization_link(
                connection_name=connector, identifier=identifier
            ).link
            print(f"    Authorize here: {link}\n")
        except Exception:
            print(f"    Check Scalekit dashboard to authorize this connector.\n")
        if sys.stdin.isatty():
            try:
                input("  Press Enter after authorizing (or Ctrl+C to skip)...")
            except (EOFError, KeyboardInterrupt):
                print(f"    Skipping {connector} -- will continue without it")
        else:
            print(f"    Continuing without {connector} (non-interactive mode)")
    else:
        print(f"  ✓ {connector} ({identifier}) -- ACTIVE")


def tool(connector: str, tool_name: str, **kwargs) -> dict:
    """Execute a Scalekit tool and return the data payload."""
    result = actions.execute_tool(
        tool_name=tool_name,
        identifier=CONNECTOR_USERS[connector],
        tool_input=kwargs,
    )
    return result.data or {}


# ── LLM classification ───────────────────────────────────────────────────────
def classify_ticket(subject: str, description: str) -> dict:
    """
    Classify a ticket into category + severity using OpenRouter LLM.
    Returns {"category": "...", "severity": "P0"-"P3", "summary": "..."}.
    Falls back to rule-based classification if LLM is unavailable.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return _classify_rule_based(subject, description)

    try:
        return _classify_with_llm(subject, description, api_key)
    except Exception as exc:
        print(f"    ⚠ LLM classification failed ({exc.__class__.__name__}: {exc}) -- using rule-based")
        return _classify_rule_based(subject, description)


def _classify_with_llm(subject: str, description: str, api_key: str) -> dict:
    import requests as http

    prompt = f"""You are a support ticket classifier. Analyze this ticket and return ONLY valid JSON with these exact keys:
- category (one of: billing, bug, feature_request, how_to, account_issue)
- severity (one of: P0, P1, P2, P3)
  P0 = service down / data loss / security breach affecting multiple users
  P1 = major feature broken, workaround exists but painful
  P2 = minor issue, cosmetic, or single-user impact
  P3 = question, enhancement idea, or low-impact ask
- summary (one sentence explaining the core issue)
- suggested_response (2-3 sentence draft reply to the customer)

Ticket subject: {subject}

Ticket description:
{description[:3000]}"""

    model = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    resp = http.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        },
        timeout=30,
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"].strip()

    # Extract JSON from markdown code fences if present
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    start, end = raw.find("{"), raw.rfind("}") + 1
    result = json.loads(raw[start:end])

    # Validate fields
    valid_categories = {"billing", "bug", "feature_request", "how_to", "account_issue"}
    valid_severities = {"P0", "P1", "P2", "P3"}
    if result.get("category") not in valid_categories:
        result["category"] = "account_issue"
    if result.get("severity") not in valid_severities:
        result["severity"] = "P2"

    print("    (LLM classification ✓)")
    return result


def _classify_rule_based(subject: str, description: str) -> dict:
    """Keyword-based fallback classifier."""
    text = f"{subject} {description}".lower()

    # Category detection
    if any(w in text for w in ("invoice", "charge", "billing", "refund", "payment", "subscription", "plan", "upgrade")):
        category = "billing"
    elif any(w in text for w in ("error", "bug", "crash", "broken", "not working", "500", "exception", "fails", "failure")):
        category = "bug"
    elif any(w in text for w in ("feature", "request", "would be nice", "suggest", "enhancement", "wishlist", "add support")):
        category = "feature_request"
    elif any(w in text for w in ("how to", "how do i", "tutorial", "guide", "documentation", "help me", "instructions")):
        category = "how_to"
    else:
        category = "account_issue"

    # Severity detection
    if any(w in text for w in ("down", "outage", "data loss", "security", "breach", "critical", "emergency", "all users")):
        severity = "P0"
    elif any(w in text for w in ("broken", "can't login", "cannot access", "blocking", "urgent", "major")):
        severity = "P1"
    elif any(w in text for w in ("minor", "cosmetic", "typo", "slow", "intermittent")):
        severity = "P2"
    else:
        severity = "P3" if category in ("feature_request", "how_to") else "P2"

    return {
        "category": category,
        "severity": severity,
        "summary": f"{category.replace('_', ' ').title()} ticket: {subject[:80]}",
        "suggested_response": "Thank you for reaching out. Our team is reviewing your request and will follow up shortly.",
    }


# ── Notion KB search ─────────────────────────────────────────────────────────
def search_notion_kb(query: str) -> list[dict]:
    """
    Search the Notion knowledge base for articles matching the query.
    Returns a list of {"title": ..., "url": ..., "snippet": ...} dicts.
    """
    if not NOTION_DB_ID:
        try:
            data = tool("notion", "notion_page_search", query=query)
        except Exception as exc:
            print(f"    ⚠ Notion search failed: {exc}")
            return []
    else:
        try:
            data = tool("notion", "notion_database_query",
                        database_id=NOTION_DB_ID, query=query)
        except Exception:
            try:
                data = tool("notion", "notion_page_search", query=query)
            except Exception as exc:
                print(f"    ⚠ Notion search failed: {exc}")
                return []

    # Normalize results from various response shapes
    results = (
        data.get("results")
        or data.get("pages")
        or data.get("data")
        or []
    )

    articles = []
    for page in results[:5]:
        title = _extract_notion_title(page)
        url = page.get("url") or page.get("public_url") or ""
        page_id = page.get("id", "")

        # Try to get a snippet from properties
        snippet = ""
        props = page.get("properties", {})
        for key in ("Description", "Summary", "Content", "Excerpt"):
            prop = props.get(key, {})
            rich_text = prop.get("rich_text", [])
            if rich_text:
                snippet = rich_text[0].get("plain_text", "")[:200]
                break

        articles.append({
            "title": title or f"Page {page_id[:8]}",
            "url": url,
            "snippet": snippet,
            "page_id": page_id,
        })

    return articles


def _extract_notion_title(page: dict) -> str:
    """Pull the title string from a Notion page object."""
    props = page.get("properties", {})

    # Check common title property names
    for key in ("Name", "Title", "name", "title"):
        prop = props.get(key, {})
        title_list = prop.get("title", [])
        if title_list:
            return title_list[0].get("plain_text", "")

    # Fallback: scan all properties for a title type
    for prop in props.values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            title_list = prop.get("title", [])
            if title_list:
                return title_list[0].get("plain_text", "")

    return ""


# ── Slack message formatting ─────────────────────────────────────────────────
def _severity_emoji(severity: str) -> str:
    return {"P0": "🔴", "P1": "🟠", "P2": "🟡", "P3": "🟢"}.get(severity, "⚪")


def build_slack_message(ticket: dict, classification: dict, kb_articles: list[dict]) -> str:
    """Build a structured Slack message for a triaged ticket."""
    cat = classification["category"]
    sev = classification["severity"]
    emoji = _severity_emoji(sev)
    raw_id = ticket.get("id", "?")
    ticket_id = str(int(raw_id)) if isinstance(raw_id, (int, float)) else str(raw_id)
    subject = ticket.get("subject") or ticket.get("raw_subject") or "No subject"
    requester = ticket.get("requester", {}).get("name") or ticket.get("requester_id") or "Unknown"

    lines = [
        f"{emoji} *[{sev}] Ticket #{ticket_id}: {subject}*",
        f"Category: `{cat}` | Severity: `{sev}` | Priority: `{SEVERITY_PRIORITY.get(sev, 'normal')}`",
        f"Requester: {requester}",
    ]

    summary = classification.get("summary", "")
    if summary:
        lines.append(f"\n> {summary}")

    if kb_articles:
        lines.append("\n📚 *Related KB Articles:*")
        for art in kb_articles[:3]:
            title = art.get("title", "Untitled")
            url = art.get("url", "")
            if url:
                lines.append(f"  • <{url}|{title}>")
            else:
                lines.append(f"  • {title}")
            if art.get("snippet"):
                lines.append(f"    _{art['snippet'][:120]}_")

    suggested = classification.get("suggested_response", "")
    if suggested:
        lines.append(f"\n💬 *Suggested Response:*\n> {suggested}")

    lines.append(f"\n_Triaged by Support Agent • {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_")
    return "\n".join(lines)


def route_to_slack(channel: str, message: str) -> dict:
    """Post a message to the designated Slack channel."""
    try:
        result = tool(SLACK_CONNECTOR, "slack_send_message",
                      channel=channel, text=message)
        return result
    except Exception as exc:
        print(f"    ⚠ Failed to post to {channel}: {exc}")
        if channel != FALLBACK_CHANNEL:
            print(f"    Retrying on {FALLBACK_CHANNEL}...")
            try:
                return tool(SLACK_CONNECTOR, "slack_send_message",
                            channel=FALLBACK_CHANNEL, text=message)
            except Exception as exc2:
                print(f"    ⚠ Fallback also failed: {exc2}")
        return {}


# ── Zendesk ticket update ────────────────────────────────────────────────────
def update_zendesk_ticket(ticket_id: str, classification: dict, kb_articles: list[dict]) -> None:
    """Add tags, set priority, and post an internal note on the Zendesk ticket."""
    cat = classification["category"]
    sev = classification["severity"]
    priority = SEVERITY_PRIORITY.get(sev, "normal")

    tags = [f"auto_category:{cat}", f"auto_severity:{sev}", "triaged_by_agent"]

    try:
        tool("zendesk", "zendesk_ticket_update",
             ticket_id=int(ticket_id),
             tags=tags,
             priority=priority)
    except Exception as exc:
        print(f"    ⚠ Failed to update ticket tags/priority: {exc}")

    # Post internal note with classification details
    note_lines = [
        f"[Auto-Triage] Category: {cat} | Severity: {sev} | Priority: {priority}",
        f"Summary: {classification.get('summary', 'N/A')}",
    ]
    if kb_articles:
        note_lines.append("Related KB articles:")
        for art in kb_articles[:3]:
            title = art.get("title", "Untitled")
            url = art.get("url", "")
            note_lines.append(f"  - {title}" + (f" ({url})" if url else ""))

    suggested = classification.get("suggested_response", "")
    if suggested:
        note_lines.append(f"Suggested response: {suggested}")

    try:
        tool("zendesk", "zendesk_ticket_reply",
             ticket_id=str(ticket_id),
             body="\n".join(note_lines),
             public=False)
    except Exception as exc:
        print(f"    ⚠ Failed to add internal note: {exc}")


# ── Ticket fetching ──────────────────────────────────────────────────────────
def fetch_new_tickets() -> list[dict]:
    """Fetch recent Zendesk tickets that have not been processed yet."""
    try:
        data = tool("zendesk", "zendesk_search_tickets",
                     query="type:ticket status:new status:open",
                     sort_by="created_at", sort_order="desc")
    except Exception:
        try:
            data = tool("zendesk", "zendesk_tickets_list",
                         sort_by="created_at", sort_order="desc")
        except Exception as exc:
            print(f"    ⚠ Failed to fetch tickets: {exc}")
            return []

    tickets = (
        data.get("results")
        or data.get("tickets")
        or data.get("data")
        or []
    )

    def _normalize_id(raw) -> str:
        return str(int(raw)) if isinstance(raw, (int, float)) else str(raw)

    return [
        t for t in tickets
        if not _is_processed(_normalize_id(t.get("id", "")))
    ]


def get_ticket_details(ticket_id: str) -> dict:
    """Fetch full ticket details including description."""
    try:
        data = tool("zendesk", "zendesk_ticket_get", ticket_id=str(ticket_id))
        return data.get("ticket", data)
    except Exception as exc:
        print(f"    ⚠ Failed to fetch ticket #{ticket_id}: {exc}")
        return {}


# ── Main triage pipeline ─────────────────────────────────────────────────────
def triage_ticket(ticket: dict) -> None:
    """Run the full triage pipeline on a single ticket."""
    raw_id = ticket.get("id", "?")
    ticket_id = str(int(raw_id)) if isinstance(raw_id, (int, float)) else str(raw_id)
    subject = ticket.get("subject") or ticket.get("raw_subject") or "No subject"
    description = ticket.get("description") or ""

    # Mark as processed BEFORE triaging. This prevents duplicate Slack alerts
    # if the process crashes mid-pipeline. The trade-off: a ticket that fails
    # mid-triage won't be auto-retried. For a support agent, no-duplicate is
    # safer than no-skip -- a missed ticket can be caught manually, but
    # duplicate alerts in Slack create confusion.
    _mark_processed(ticket_id)

    if not description:
        full = get_ticket_details(ticket_id)
        description = full.get("description") or ""
        ticket = {**ticket, **full}

    # ── Classify ──────────────────────────────────────────────────────────
    print(f"\n── Step 2: Classifying ticket ──")
    print(f"  Ticket #{ticket_id}: \"{subject}\"")

    classification = classify_ticket(subject, description)
    cat = classification["category"]
    sev = classification["severity"]
    print(f"  Category: {cat} | Severity: {sev}")

    # ── Search Notion KB ──────────────────────────────────────────────────
    print(f"\n── Step 3: Searching Notion KB ──")
    kb_articles: list[dict] = []
    if cat in ("how_to", "bug", "account_issue"):
        # Use the LLM-generated summary as the search query instead of the raw
        # ticket subject. Subjects are often vague ("help!!", "it's broken again")
        # while the summary captures the actual issue in searchable terms.
        search_query = classification.get("summary") or subject
        kb_articles = search_notion_kb(search_query)
        if kb_articles:
            print(f"  Found {len(kb_articles)} matching article(s)")
            for art in kb_articles[:3]:
                print(f"    • {art['title']}")
        else:
            print(f"  No matching articles found")
    else:
        print(f"  Skipped (category '{cat}' does not require KB search)")

    # ── Route to Slack ────────────────────────────────────────────────────
    print(f"\n── Step 4: Routing to Slack ──")
    channel = CHANNEL_MAP.get(cat, FALLBACK_CHANNEL)
    message = build_slack_message(ticket, classification, kb_articles)
    result = route_to_slack(channel, message)
    if result:
        print(f"  ✓ Posted to {channel}")
    else:
        print(f"  ✗ Failed to post to {channel}")

    # ── Update Zendesk ────────────────────────────────────────────────────
    print(f"\n── Step 5: Updating Zendesk ticket ──")
    update_zendesk_ticket(ticket_id, classification, kb_articles)
    print(f"  ✓ Tags added, internal note created")


def run_once() -> int:
    """Run a single triage cycle. Returns the number of tickets processed."""
    print(f"\n── Step 1: Fetching new Zendesk tickets ──")
    tickets = fetch_new_tickets()
    print(f"  Found {len(tickets)} new ticket(s)")

    if not tickets:
        return 0

    for i, ticket in enumerate(tickets):
        triage_ticket(ticket)
        print()
        # Brief pause between tickets to respect API rate limits
        # (Slack: 1 msg/sec/channel, Notion: 3 req/sec, Zendesk: plan-dependent)
        if i < len(tickets) - 1:
            time.sleep(1.5)

    return len(tickets)


def main() -> None:
    _load_processed_ids()

    # ── Step 0: Check auth ────────────────────────────────────────────────
    print("\n── Step 0: Checking connector auth ──")
    for connector in ("zendesk", SLACK_CONNECTOR, "notion"):
        ensure_authorized(connector)

    if POLLING_MODE:
        print(f"\n🔄 Polling mode enabled (interval: {POLL_INTERVAL_MINUTES}m)")
        print(f"   Press Ctrl+C to stop.\n")
        cycle = 0
        while True:
            cycle += 1
            print(f"\n{'=' * 60}")
            print(f"  Polling cycle #{cycle} -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
            print(f"{'=' * 60}")

            try:
                count = run_once()
                if count:
                    print(f"\n✓ Processed {count} ticket(s) this cycle.")
                else:
                    print(f"\n  No new tickets. Sleeping {POLL_INTERVAL_MINUTES}m...")
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"\n  ⚠ Error during cycle: {exc.__class__.__name__}: {exc}")

            try:
                time.sleep(POLL_INTERVAL_MINUTES * 60)
            except KeyboardInterrupt:
                print("\n\n✓ Polling stopped.")
                break
    else:
        count = run_once()
        if count:
            print(f"\n✓ Flow complete. Processed {count} ticket(s).\n")
        else:
            print(f"\n✓ No new tickets to process.\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n✓ Agent stopped.\n")
        sys.exit(0)
