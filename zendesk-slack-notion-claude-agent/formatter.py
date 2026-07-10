"""
Message formatting for Slack and Zendesk.

Builds rich, structured messages with classification details,
KB articles, and suggested responses.
"""

from datetime import datetime, timezone
from typing import Dict, List


def severity_emoji(severity: str) -> str:
    """Get emoji for severity level."""
    return {
        "P0": "🔴",
        "P1": "🟠",
        "P2": "🟡",
        "P3": "🟢",
    }.get(severity, "⚪")


def build_slack_message(
    ticket: Dict,
    classification: Dict,
    kb_articles: List[Dict],
    severity_priority: Dict,
) -> str:
    """Build a structured Slack message for a triaged ticket."""
    cat = classification["category"]
    sev = classification["severity"]
    emoji = severity_emoji(sev)
    raw_id = ticket.get("id", "?")
    ticket_id = str(int(raw_id)) if isinstance(raw_id, (int, float)) else str(raw_id)
    subject = ticket.get("subject") or ticket.get("raw_subject") or "No subject"
    requester = ticket.get("requester", {}).get("name") or ticket.get("requester_id") or "Unknown"

    lines = [
        f"{emoji} *[{sev}] Ticket #{ticket_id}: {subject}*",
        f"Category: `{cat}` | Severity: `{sev}` | Priority: `{severity_priority.get(sev, 'normal')}`",
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


def build_zendesk_internal_note(
    classification: Dict,
    kb_articles: List[Dict],
    severity_priority: Dict,
) -> str:
    """Build internal note for Zendesk ticket."""
    cat = classification["category"]
    sev = classification["severity"]
    priority = severity_priority.get(sev, "normal")

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

    return "\n".join(note_lines)
