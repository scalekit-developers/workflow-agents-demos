# parsers.py — LLM-based email intent + entity extraction

from __future__ import annotations
import json
import os
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pytz

from entities import Attendee, ParsedEmail

import anthropic as _anthropic

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
HTML_TAG_RE = re.compile(r"<[^>]+>")
EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)

_anthropic_client: Optional[_anthropic.Anthropic] = None


def _get_anthropic() -> Optional[_anthropic.Anthropic]:
    global _anthropic_client
    if not ANTHROPIC_API_KEY:
        return None
    if _anthropic_client is None:
        _anthropic_client = _anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_client


def _strip_html(text: str | None) -> str:
    return HTML_TAG_RE.sub(" ", text or "")


def _extract_emails_from_headers(headers: Dict) -> List[str]:
    seen = set()
    emails = []
    for key in ("To", "Cc", "From", "to", "cc", "from"):
        for e in EMAIL_RE.findall(headers.get(key) or ""):
            if e.lower() not in seen:
                seen.add(e.lower())
                emails.append(e)
    return emails


def _llm_extract(subject: str, body: str, header_emails: List[str],
                 user_tz: str, today_iso: str) -> Optional[Dict]:
    """Call Claude to extract scheduling intent and entities."""
    client = _get_anthropic()
    if not client:
        return None

    prompt = f"""Today is {today_iso}. The user's timezone is {user_tz}.

Email subject: {subject}
Email body (first 1500 chars): {body[:1500]}
Email header contacts: {', '.join(header_emails)}

Does this email contain a scheduling request (meeting invite, "let's sync", "can we meet", calendar event, etc.)?

If YES, extract and return ONLY this JSON:
{{
  "is_scheduling": true,
  "title": "<meeting title>",
  "duration_minutes": <int, default 30>,
  "start_datetime": "<ISO 8601 with timezone, or null if unknown>",
  "end_datetime": "<ISO 8601 with timezone, or null if unknown>",
  "timezone": "<IANA tz string, e.g. Asia/Kolkata>",
  "attendees": ["email1@example.com", "email2@example.com"]
}}

If NO (not a scheduling email), return ONLY:
{{"is_scheduling": false}}

Return raw JSON only, no explanation."""

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S).strip()
    return json.loads(raw)


def parse_entities(subject: str, body_raw: str, headers: Dict, *,
                   user_tz: str = "Asia/Kolkata", default_duration: int = 30) -> ParsedEmail:
    body = _strip_html(body_raw)
    header_emails = _extract_emails_from_headers(headers)
    today_iso = datetime.now(pytz.timezone(user_tz)).date().isoformat()

    extracted = _llm_extract(subject, body, header_emails, user_tz, today_iso)

    # If LLM says not a scheduling email or returned nothing, return empty ParsedEmail
    if not extracted or not extracted.get("is_scheduling"):
        return ParsedEmail(title=subject or "Meeting", attendees=[])

    tz_str = extracted.get("timezone") or user_tz
    local_tz = pytz.timezone(tz_str)

    hard_start: Optional[datetime] = None
    hard_end: Optional[datetime] = None

    if extracted.get("start_datetime"):
        try:
            hard_start = datetime.fromisoformat(extracted["start_datetime"])
            if not hard_start.tzinfo:
                hard_start = local_tz.localize(hard_start)
        except Exception:
            hard_start = None

    if extracted.get("end_datetime"):
        try:
            hard_end = datetime.fromisoformat(extracted["end_datetime"])
            if not hard_end.tzinfo:
                hard_end = local_tz.localize(hard_end)
        except Exception:
            hard_end = None

    duration = int(extracted.get("duration_minutes") or default_duration)
    if hard_start and not hard_end:
        hard_end = hard_start + timedelta(minutes=duration)
    if hard_start and hard_end:
        duration = max(1, int((hard_end - hard_start).total_seconds() // 60))

    # Merge LLM attendees + header emails, deduplicate
    seen: set = set()
    attendees: List[Attendee] = []
    for e in (extracted.get("attendees") or []) + header_emails:
        if e.lower() not in seen:
            seen.add(e.lower())
            attendees.append(Attendee(email=e))

    return ParsedEmail(
        title=extracted.get("title") or subject or "Meeting",
        duration_minutes=duration,
        tz_hint=tz_str,
        hard_start=hard_start,
        hard_end=hard_end,
        attendees=attendees,
    )
