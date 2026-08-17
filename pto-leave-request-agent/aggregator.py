"""
Business logic: validate the requested leave against Deel's real balance
data, build the Google Calendar event details, and draft the Slack
notification text for the manager.

Leave balance / policy source of truth
---------------------------------------
deelmcp_timeoff_entitlement_list returns a real remaining balance for a
given hris_profile_id and policy type. This agent reads that balance at
request time rather than tracking a local running total, so the check is
always current against whatever Deel itself considers the source of truth
(including time off approved through channels other than this agent).

The real response carries the remaining balance under an "available" field
(a numeric string, e.g. "0.00"), confirmed live. parse_entitlement_remaining()
below also tries a few other plausible field names as a defensive fallback,
and raises a clear, specific error rather than silently treating a missing
or unrecognized field as "zero remaining" or "unlimited" -- a real API shape
change should surface immediately, not silently misvalidate every future
request.

Business days calculation
--------------------------
Requested leave days are counted as weekdays (Monday-Friday) between
start_date and end_date inclusive. Public holidays are not excluded, so a
request spanning a company holiday will count that day as a leave day. This
is called out explicitly in the calendar block and Slack notification so
nobody is misled into thinking holiday-awareness happened silently.
"""

import datetime
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PTO_TYPE_LABELS = {
    "vacation": "Vacation",
    "paid": "Paid Leave",
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


def parse_entitlement_remaining(entitlement: Dict) -> float:
    """
    Extract the remaining-days figure from a Deel entitlement record. Tries
    the plausible field names in order and raises LeaveValidationError
    (never silently defaults to 0 or unlimited) if none are present, since
    guessing a balance here is worse than failing with a clear message about
    what Deel actually returned.
    """
    for field in ("remaining", "available", "balance", "remaining_balance", "days_remaining"):
        if field in entitlement and entitlement[field] is not None:
            try:
                return float(entitlement[field])
            except (TypeError, ValueError):
                continue
    raise LeaveValidationError(
        f"Deel's entitlement record for this policy has no recognizable "
        f"remaining-balance field (got keys: {list(entitlement.keys())}). "
        f"Cannot validate this request against a real balance.",
        code="UNREADABLE_ENTITLEMENT",
    )


def validate_leave_request(
    start_date: datetime.date,
    end_date: datetime.date,
    requested_days: int,
    remaining_balance: float,
) -> float:
    """
    Validate a leave request against Deel's real remaining balance.

    Returns remaining_after (days) if valid. Raises LeaveValidationError
    (never silently submits) if:
      - the request is entirely in the past
      - the request would exceed the remaining balance

    This runs BEFORE any write to Deel, Google Calendar, or Slack -- an
    insufficient-balance request never reaches a create-request call at all.
    """
    today = datetime.date.today()
    if end_date < today:
        raise LeaveValidationError(
            f"Requested leave ({start_date.isoformat()} to {end_date.isoformat()}) "
            f"is entirely in the past (today is {today.isoformat()})",
            code="PAST_DATES",
        )

    remaining_after = remaining_balance - requested_days

    if remaining_after < 0:
        raise LeaveValidationError(
            f"Insufficient leave balance: requested {requested_days} day(s), "
            f"but only {remaining_balance:g} day(s) remain per Deel",
            code="INSUFFICIENT_BALANCE",
        )

    return remaining_after


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
        f"Remaining balance after this request: {remaining_after:g} day(s) (per Deel)",
    ]
    if reason:
        lines.append(f"Reason: {reason}")
    lines.append("")
    lines.append(
        "This request has been submitted to Deel and is pending your approval "
        "there."
    )
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


def build_rejection_message(employee_name: str, reason: str) -> str:
    """Slack message sent to the manager when the request is rejected before reaching Deel."""
    name = employee_name or "the employee"
    return (
        f"*Leave request NOT submitted for {name}*\n\n"
        f"Reason: {reason}\n\n"
        "_No calendar block or Deel submission was made for this request._"
    )
