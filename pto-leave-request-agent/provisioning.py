"""
Startup provisioning: confirm the employee is a findable person in Gusto
(employee or contractor record) and that the Google Calendar destination is
reachable, before attempting any leave submission.

Gusto's connected tool surface has no time-off-balance or time-off-policy
tool to validate against (see connectors.py module docstring for the full,
live-verified enumeration), so this module's job is narrower than the
sibling agents' provisioning steps: it confirms IDENTITY only (the
EMPLOYEE_EMAIL configured resolves to a real person in this Gusto company),
not balance/policy, which is handled by the configured entitlement in
aggregator.py + state.py instead.
"""

import logging

from connectors import ConnectorError, GoogleCalendarConnector, GustoConnector

logger = logging.getLogger(__name__)


class ProvisioningError(Exception):
    """Raised when required setup is missing and cannot be auto-created."""


def resolve_employee(gusto: GustoConnector, employee_email: str, employee_gusto_uuid: str = "") -> dict:
    """
    Confirm `employee_email` resolves to a real employee or contractor record
    in Gusto. Raises ProvisioningError if not found or if Gusto can't be
    reached at all -- without this, there is no reliable identity check
    before blocking someone's calendar and DMing their manager.

    If EMPLOYEE_GUSTO_UUID is set, fetches that record directly (tried as an
    employee UUID first, then a contractor UUID) instead of listing and
    matching by email, which is faster and avoids ambiguity when two people
    share a display name.
    """
    if employee_gusto_uuid:
        try:
            person = gusto.get_employee(employee_gusto_uuid)
            if person and person.get("uuid"):
                person["gusto_person_type"] = "employee"
                return person
        except ConnectorError:
            pass
        try:
            person = gusto.get_contractor(employee_gusto_uuid)
            if person and person.get("uuid"):
                person["gusto_person_type"] = "contractor"
                return person
        except ConnectorError as e:
            raise ProvisioningError(
                f"EMPLOYEE_GUSTO_UUID '{employee_gusto_uuid}' did not resolve to an "
                f"employee or contractor record in Gusto: {e}"
            ) from e
        raise ProvisioningError(
            f"EMPLOYEE_GUSTO_UUID '{employee_gusto_uuid}' did not resolve to an "
            f"employee or contractor record in Gusto."
        )

    try:
        person = gusto.find_person_by_email(employee_email)
    except ConnectorError as e:
        raise ProvisioningError(
            f"Cannot reach Gusto to look up '{employee_email}': {e}\n"
            f"Confirm GUSTO_CONNECTOR points at an ACTIVE Gusto connection."
        ) from e

    if not person:
        raise ProvisioningError(
            f"'{employee_email}' was not found as an employee or contractor in "
            f"this Gusto company. Confirm EMPLOYEE_EMAIL matches the email on "
            f"file in Gusto exactly, or set EMPLOYEE_GUSTO_UUID directly if you "
            f"already know the person's Gusto UUID."
        )

    person_type = person.get("gusto_person_type", "person")
    logger.info(
        f"[OK] Resolved '{employee_email}' as a Gusto {person_type} "
        f"(uuid={person.get('uuid', '?')})"
    )
    return person


def verify_calendar_access(calendar: GoogleCalendarConnector, calendar_id: str) -> None:
    """
    Confirm the configured Google Calendar is reachable before attempting to
    create a leave block on it, by listing a narrow, harmless time window.
    Raises ProvisioningError if the calendar can't be read at all.
    """
    import datetime

    today = datetime.date.today()
    time_min = today.isoformat() + "T00:00:00Z"
    time_max = (today + datetime.timedelta(days=1)).isoformat() + "T00:00:00Z"

    try:
        calendar.list_events(calendar_id, time_min, time_max)
    except ConnectorError as e:
        raise ProvisioningError(
            f"Cannot access Google Calendar '{calendar_id}': {e}\n"
            f"Confirm GOOGLE_CALENDAR_CONNECTOR points at an ACTIVE Google "
            f"Calendar connection for GOOGLE_CALENDAR_USER, and that "
            f"GOOGLE_CALENDAR_ID is either 'primary' or a calendar ID this "
            f"identity has write access to."
        ) from e

    logger.info(f"[OK] Google Calendar '{calendar_id}' is reachable")
