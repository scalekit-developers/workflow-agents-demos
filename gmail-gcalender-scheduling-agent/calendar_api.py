# calendar_api.py — Google Calendar via Scalekit connector tools

from __future__ import annotations
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple
import pytz

from sk_connectors import execute_tool
from slotting import derive_busy, overlaps, iso

PRIMARY_CALENDAR_ID = "primary"


def get_primary_calendar_id(identifier: str) -> str:
    try:
        data = execute_tool("googlecalendar_list_calendars", {}, identifier)
    except Exception:
        return PRIMARY_CALENDAR_ID

    cals = []
    if isinstance(data, dict):
        cals = data.get("calendars") or data.get("items") or []
    elif isinstance(data, list):
        cals = data
    for c in cals:
        if isinstance(c, dict) and str(c.get("primary", "")).lower() == "true":
            return c.get("id", PRIMARY_CALENDAR_ID)
    return cals[0].get("id", PRIMARY_CALENDAR_ID) if cals else PRIMARY_CALENDAR_ID


def list_events(identifier: str, calendar_id: str,
                time_min: str, time_max: str, max_results: int = 250) -> List[Dict]:
    data = execute_tool(
        "googlecalendar_list_events",
        {
            "calendarId": calendar_id,
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": max_results,
        },
        identifier,
    )
    if isinstance(data, dict):
        return data.get("items") or data.get("events") or []
    if isinstance(data, list):
        return data
    return []


def get_busy_slots(identifier: str, time_min: str, time_max: str,
                   local_tz: pytz.BaseTzInfo) -> List[Tuple[datetime, datetime]]:
    cal_id = get_primary_calendar_id(identifier)
    events = list_events(identifier, cal_id, time_min, time_max)
    return derive_busy(events, local_tz)


def create_event(identifier: str, calendar_id: str, event: Dict[str, Any]) -> Dict:
    start_dt = (event.get("start") or {}).get("dateTime") or event.get("start_datetime")
    end_dt   = (event.get("end")   or {}).get("dateTime") or event.get("end_datetime")
    tz       = ((event.get("start") or {}).get("timeZone")
                or (event.get("end") or {}).get("timeZone")
                or event.get("time_zone", "UTC"))

    if not start_dt or not end_dt:
        raise ValueError("create_event: start/end datetime required")

    # Conflict check — query only around the requested event window
    local_tz = pytz.timezone(tz)
    s = datetime.fromisoformat(start_dt)
    e = datetime.fromisoformat(end_dt)
    if not s.tzinfo:
        s = local_tz.localize(s)
    if not e.tzinfo:
        e = local_tz.localize(e)
    busy = get_busy_slots(
        identifier,
        iso(s - timedelta(hours=1)),
        iso(e + timedelta(hours=1)),
        local_tz,
    )
    for b0, b1 in busy:
        if overlaps(s, e, b0, b1):
            return {"error": "Conflict with existing event — choose another time."}

    attendees = []
    for a in event.get("attendees") or []:
        if isinstance(a, dict) and a.get("email"):
            attendees.append({"email": a["email"], "optional": bool(a.get("optional", False))})
        elif isinstance(a, str):
            attendees.append({"email": a})

    data = execute_tool(
        "googlecalendar_create_event",
        {
            "calendarId": calendar_id,
            "summary": event.get("summary", "Meeting"),
            "description": event.get("description", ""),
            "start_datetime": start_dt,
            "end_datetime": end_dt,
            "time_zone": tz,
            "startDateTime": start_dt,
            "endDateTime": end_dt,
            "timeZone": tz,
            "attendees": attendees,
            "sendUpdates": event.get("sendUpdates", "all"),
        },
        identifier,
    )
    if not isinstance(data, dict):
        return {}
    # Scalekit wraps the response: {"event": {...}} — unwrap it
    return data.get("event") or data
