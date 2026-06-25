from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Attendee(BaseModel):
    email: str
    optional: bool = False


class ParsedEmail(BaseModel):
    title: str = "Meeting"
    duration_minutes: int = 30
    tz_hint: str = "UTC"
    hard_start: datetime | None = None
    hard_end: datetime | None = None
    attendees: list[Attendee] = Field(default_factory=list)
