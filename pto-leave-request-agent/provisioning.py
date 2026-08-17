"""
Startup provisioning: confirm the employee is a findable person in Deel
(with a resolvable hris_profile_id and an assigned policy for the requested
PTO type), that the requested Deel policy type name is a real, recognized
type on this platform, and that the Google Calendar destination is
reachable, before attempting any leave submission.
"""

import logging

from connectors import ConnectorError, DeelConnector, GoogleCalendarConnector

logger = logging.getLogger(__name__)


class ProvisioningError(Exception):
    """Raised when required setup is missing and cannot be auto-created."""


def resolve_employee(deel: DeelConnector, employee_email: str, employee_deel_profile_id: str = "") -> dict:
    """
    Confirm `employee_email` resolves to a real worker in Deel with a usable
    hris_profile_id. Raises ProvisioningError if not found or if Deel can't
    be reached at all -- without this, there is no reliable identity check
    before blocking someone's calendar and submitting a time-off request on
    their behalf.

    If EMPLOYEE_DEEL_PROFILE_ID is set, it's used directly instead of
    listing every contract and matching by email, which is faster and
    avoids ambiguity when two people share a display name.
    """
    if employee_deel_profile_id:
        return {"hris_profile_id": employee_deel_profile_id, "email": employee_email}

    try:
        person = deel.find_person_by_email(employee_email)
    except ConnectorError as e:
        raise ProvisioningError(
            f"Cannot reach Deel to look up '{employee_email}': {e}\n"
            f"Confirm DEEL_CONNECTOR points at an ACTIVE Deel connection."
        ) from e

    if not person:
        raise ProvisioningError(
            f"'{employee_email}' was not found among this Deel organization's "
            f"onboarded workers. Confirm EMPLOYEE_EMAIL matches the email on "
            f"file in Deel exactly, that the worker has completed onboarding "
            f"(a contract still in progress has no resolvable worker yet), or "
            f"set EMPLOYEE_DEEL_PROFILE_ID directly if you already know the "
            f"person's hris_profile_id."
        )

    profile_id = person.get("hris_profile_id") or person.get("id")
    if not profile_id:
        raise ProvisioningError(
            f"Found '{employee_email}' in Deel, but their record has no "
            f"hris_profile_id. Set EMPLOYEE_DEEL_PROFILE_ID directly if you "
            f"know it."
        )

    logger.info(f"[OK] Resolved '{employee_email}' in Deel (hris_profile_id={profile_id})")
    person["hris_profile_id"] = profile_id
    return person


def resolve_policy(deel: DeelConnector, hris_profile_id: str, deel_policy_type_name: str) -> dict:
    """
    Confirm the employee has a policy assigned for the requested PTO type
    (mapped to Deel's policy_type_name, see config.py), and return it.
    Raises ProvisioningError if no such policy is assigned -- without a real
    policy_id, deelmcp_timeoff_request_create cannot be called correctly,
    and guessing one would be worse than failing clearly.
    """
    try:
        policy = deel.get_policy_for_type(hris_profile_id, deel_policy_type_name)
    except ConnectorError as e:
        raise ProvisioningError(
            f"Cannot reach Deel to look up time-off policies for this employee: {e}"
        ) from e

    if not policy or not policy.get("id"):
        raise ProvisioningError(
            f"This employee has no '{deel_policy_type_name}' time-off policy "
            f"assigned in Deel. Confirm PTO_TYPE maps to a policy type your "
            f"Deel organization actually has configured for this person, or "
            f"assign one in the Deel dashboard first."
        )

    logger.info(f"[OK] Resolved '{deel_policy_type_name}' policy (policy_id={policy['id']})")
    return policy


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
