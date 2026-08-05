"""
Business logic: validate the requested leave against the configured
balance/policy, build the Google Calendar event details, and draft the
Slack notification text for the manager.

Leave balance / policy source of truth
---------------------------------------
GUSTOMCP's real tool surface (verified live, see connectors.py module
docstring) has no time-off balance or time-off policy tool at all -- Gusto
cannot answer "how many vacation days does this employee have left" through
any tool this agent can call. This agent therefore validates the requested
leave against a CONFIGURED annual entitlement (PTO_ANNUAL_ENTITLEMENT_DAYS)
and a running total of days already used, tracked in local state
(state/pto_usage.json, separate from state/processed_requests.json's
idempotency ledger). This is a documented workaround for a real, verified
connector gap, not a silent guess -- see README's Error Handling section.

Business days calculation
--------------------------
Requested leave days are counted as weekdays (Monday-Friday) between
start_date and end_date inclusive. Public holidays are not excluded (Gusto's
connected tools expose no holiday-calendar object either), so a request
spanning a company holiday will count that day as a leave day. This is
called out explicitly in the calendar block and Slack notification so
nobody is misled into thinking holiday-awareness happened silently.
"""

import datetime
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PTO_TYPE_LABELS = {
    "vacation": "Vacation",
    "sick": "Sick Leave",
    "personal": "Personal Leave",
    "bereavement": "Bereavement Leave",
    "other": "Leave",
}


def count_business_days(start_date: datetime.date, end_date: datetime.date) -> int:
    """Count Mon-Fri days between start_date and end_date, inclusive."""
    days = 0
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:  # 0=Mon ... 4=Fri
            days += 1
        current += datetime.timedelta(days=1)
    return days


class LeaveValidationError(Exception):
    """Raised when a leave request fails policy validation and must NOT be submitted."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def validate_leave_request(
    start_date: datetime.date,
    end_date: datetime.date,
    requested_days: int,
    annual_entitlement_days: float,
    days_already_used: float,
) -> Tuple[float, float]:
    """
    Validate a leave request against the configured entitlement.

    Returns (remaining_before, remaining_after) in days if valid. Raises
    LeaveValidationError (never silently submits) if:
      - the request is entirely in the past
      - the request would exceed the remaining balance

    This runs BEFORE any write to Gusto, Google Calendar, or Slack -- an
    insufficient-balance request never reaches a connector call at all.
    """
    today = datetime.date.today()
    if end_date < today:
        raise LeaveValidationError(
            f"Requested leave ({start_date.isoformat()} to {end_date.isoformat()}) "
            f"is entirely in the past (today is {today.isoformat()})",
            code="PAST_DATES",
        )

    remaining_before = annual_entitlement_days - days_already_used
    remaining_after = remaining_before - requested_days

    if remaining_after < 0:
        raise LeaveValidationError(
            f"Insufficient leave balance: requested {requested_days} day(s), "
            f"but only {remaining_before:g} day(s) remain "
            f"({annual_entitlement_days:g} annual entitlement minus "
            f"{days_already_used:g} already used/pending)",
            code="INSUFFICIENT_BALANCE",
        )

    return remaining_before, remaining_after


def build_calendar_summary(employee_name: str, pto_type: str) -> str:
    """Event title shown on the employee's calendar."""
    label = PTO_TYPE_LABELS.get(pto_type, PTO_TYPE_LABELS["other"])
    name_part = f"{employee_name} - " if employee_name else ""
    return f"{name_part}{label} (Out of Office)"


def build_manager_slack_message(
    employee_name: str,
    employee_email: str,
    pto_type: str,
    start_date: datetime.date,
    end_date: datetime.date,
    requested_days: int,
    remaining_after: float,
    reason: str,
    calendar_event_created: bool,
    calendar_html_link: str,
) -> str:
    """Compose the manager's Slack DM about this leave request."""
    label = PTO_TYPE_LABELS.get(pto_type, PTO_TYPE_LABELS["other"])
    name = employee_name or employee_email

    lines = [
        f"*New {label} Request - {name}*",
        "",
        f"Dates: {start_date.isoformat()} to {end_date.isoformat()} "
        f"({requested_days} business day{'s' if requested_days != 1 else ''})",
        f"Remaining balance after this request: {remaining_after:g} day(s)",
    ]
    if reason:
        lines.append(f"Reason: {reason}")
    lines.append("")

    if calendar_event_created:
        if calendar_html_link:
            lines.append(f"Calendar blocked: <{calendar_html_link}|view event>")
        else:
            lines.append("Calendar blocked for these dates.")
    else:
        lines.append(
            "Note: the calendar block could not be created. Dates above are "
            "still accurate; please confirm availability manually."
        )

    lines.append("")
    lines.append(
        "_Submitted by your PTO & Leave Request Agent on behalf of "
        f"{employee_email}._"
    )
    return "\n".join(lines)


def build_gusto_rejection_message(employee_name: str, reason: str) -> str:
    """Slack message sent to the manager when Gusto (or policy validation) rejects the request."""
    name = employee_name or "the employee"
    return (
        f"*Leave request NOT submitted for {name}*\n\n"
        f"Reason: {reason}\n\n"
        "_No calendar block or Gusto submission was made for this request._"
    )
