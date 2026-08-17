"""
Configuration management with validation.

All settings loaded from environment variables.
Validates on startup and provides clear error messages.
"""

import datetime
import logging
import os
import sys
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# This agent's own PTO_TYPE taxonomy, mapped to Deel's real policy_type_name
# enum (verified live against deelmcp_timeoff_policy_list's input_schema).
# Kept as a small, human-readable set rather than exposing Deel's full
# 29-value enum (which includes country-specific types like "Hajj leave",
# "RTT", "Regional holiday") directly as CLI/env input. "paid" is included
# because Deel's own default general-purpose policy type in a freshly set up
# organization is named "Paid leave" (verified live against a real assigned
# policy), distinct from "Vacation" -- which Vacation/other name applies
# depends entirely on how your organization's Deel policies are configured,
# so confirm with `deelmcp_timeoff_policy_list` for a specific employee
# rather than assuming.
VALID_PTO_TYPES = ("vacation", "paid", "sick", "personal", "bereavement", "other")

PTO_TYPE_TO_DEEL_POLICY_TYPE = {
    "vacation": "Vacation",
    "paid": "Paid leave",
    "sick": "Sick leave",
    "personal": "Personal leave",
    "bereavement": "Bereavement leave",
    "other": "Other leave",
}


class Config:
    """Application configuration."""

    def __init__(self):
        """Load configuration from environment variables."""
        # Scalekit
        self.scalekit_env_url = os.environ.get("SCALEKIT_ENV_URL")
        self.scalekit_client_id = os.environ.get("SCALEKIT_CLIENT_ID")
        self.scalekit_client_secret = os.environ.get("SCALEKIT_CLIENT_SECRET")

        # Connector identities (the "identifier" each connected account is keyed by)
        self.deel_user = os.environ.get("DEEL_USER")
        self.google_calendar_user = os.environ.get("GOOGLE_CALENDAR_USER")
        self.slack_user = os.environ.get("SLACK_USER")

        # Connector names -- must match the exact connection name shown in the
        # Scalekit dashboard, which is often auto-suffixed per workspace
        # (e.g. "deelmcp-zTWsHKTh"), not just the generic provider label.
        self.deel_connector = os.environ.get("DEEL_CONNECTOR", "deelmcp-zTWsHKTh")
        self.google_calendar_connector = os.environ.get("GOOGLE_CALENDAR_CONNECTOR", "googlecalendar")
        self.slack_connector = os.environ.get("SLACK_CONNECTOR", "slackmcp")

        # The employee whose PTO this run is for (the delegated identity this
        # agent acts on behalf of). Deel's time-off tools are scoped by
        # hris_profile_id (a UUID), not email -- see connectors.py's
        # DeelConnector.find_person_by_email for how this is resolved.
        self.employee_email = os.environ.get("EMPLOYEE_EMAIL")
        self.employee_name = os.environ.get("EMPLOYEE_NAME", "")

        # Optional: if you already know the employee's Deel hris_profile_id,
        # set it to skip the list-every-contract-and-match-by-email lookup
        # in provisioning.py -- faster, and avoids ambiguity when two people
        # share a display name.
        self.employee_deel_profile_id = os.environ.get("EMPLOYEE_DEEL_PROFILE_ID", "")

        # Manager notified via Slack DM. Resolved to a Slack user ID via
        # slackmcp_slack_search_users if MANAGER_SLACK_ID isn't set directly.
        self.manager_email = os.environ.get("MANAGER_EMAIL")
        self.manager_slack_id = os.environ.get("MANAGER_SLACK_ID", "")

        # The leave request itself.
        self.pto_start_date = os.environ.get("PTO_START_DATE", "")
        self.pto_end_date = os.environ.get("PTO_END_DATE", "")
        self.pto_type = os.environ.get("PTO_TYPE", "vacation").strip().lower()
        self.pto_reason = os.environ.get("PTO_REASON", "")

        # Google Calendar destination for the PTO block.
        self.google_calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

        # Timing / mode. A single PTO submission is inherently a one-shot
        # action (there is exactly one request to submit, not an open-ended
        # feed to keep polling), so POLLING_MODE here means "keep checking a
        # local queue of pending PTO requests and process any that haven't
        # been submitted yet" rather than blindly re-running the same
        # request on a timer. See run_flow.py and state.py.
        self.polling_mode = os.environ.get("POLLING_MODE", "false").lower() == "true"
        self.poll_interval_minutes = self._parse_int("POLL_INTERVAL_MINUTES", 15, min_value=1)
        self.log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    def validate(self):
        """Validate required configuration. Fails fast if missing."""
        errors = []

        if not self.scalekit_env_url:
            errors.append("SCALEKIT_ENV_URL")
        if not self.scalekit_client_id:
            errors.append("SCALEKIT_CLIENT_ID")
        if not self.scalekit_client_secret:
            errors.append("SCALEKIT_CLIENT_SECRET")

        if not self.deel_user:
            errors.append("DEEL_USER")
        if not self.google_calendar_user:
            errors.append("GOOGLE_CALENDAR_USER")
        if not self.slack_user:
            errors.append("SLACK_USER")

        if not self.employee_email:
            errors.append("EMPLOYEE_EMAIL")
        if not self.manager_email and not self.manager_slack_id:
            errors.append("MANAGER_EMAIL or MANAGER_SLACK_ID")

        if not self.pto_start_date:
            errors.append("PTO_START_DATE")
        if not self.pto_end_date:
            errors.append("PTO_END_DATE")

        if errors:
            msg = f"Missing required config: {', '.join(errors)}"
            logger.error(msg)
            sys.exit(1)

        if self.pto_type not in VALID_PTO_TYPES:
            msg = (
                f"Invalid PTO_TYPE: {self.pto_type!r}. Must be one of: "
                f"{', '.join(VALID_PTO_TYPES)}"
            )
            logger.error(msg)
            sys.exit(1)

        self._validate_dates()

    def deel_policy_type_name(self) -> str:
        """The Deel policy_type_name this run's PTO_TYPE maps to."""
        return PTO_TYPE_TO_DEEL_POLICY_TYPE[self.pto_type]

    def _validate_dates(self):
        """Parse and sanity-check PTO_START_DATE / PTO_END_DATE. Fails fast on bad input."""
        try:
            start = datetime.date.fromisoformat(self.pto_start_date)
        except ValueError:
            msg = f"Invalid PTO_START_DATE: {self.pto_start_date!r} (expected YYYY-MM-DD)"
            self._fatal(msg)
            return

        try:
            end = datetime.date.fromisoformat(self.pto_end_date)
        except ValueError:
            msg = f"Invalid PTO_END_DATE: {self.pto_end_date!r} (expected YYYY-MM-DD)"
            self._fatal(msg)
            return

        if end < start:
            self._fatal(
                f"PTO_END_DATE ({end.isoformat()}) is before PTO_START_DATE "
                f"({start.isoformat()})"
            )
            return

        # This is intentionally checked here (not just downstream in
        # aggregator.py) so a plainly invalid request never reaches a live
        # connector call at all -- fail fast on obviously bad input.
        today = datetime.date.today()
        if end < today:
            self._fatal(
                f"PTO_END_DATE ({end.isoformat()}) is entirely in the past "
                f"(today is {today.isoformat()}). Refusing to submit a leave "
                f"request for dates that have already elapsed."
            )
            return

    def _fatal(self, msg: str):
        logger.error(msg)
        sys.exit(1)

    def get_connector_users(self) -> List[tuple]:
        """
        List of (connector name, identifier) pairs, for auth checks. A list
        rather than a dict so two connectors that happen to share a
        connection name both still get checked, instead of one silently
        overwriting the other as a dict key would.
        """
        return [
            (self.deel_connector, self.deel_user),
            (self.google_calendar_connector, self.google_calendar_user),
            (self.slack_connector, self.slack_user),
        ]

    @staticmethod
    def _parse_int(key: str, default: int, min_value: Optional[int] = None) -> int:
        raw = os.environ.get(key, str(default))
        try:
            value = int(raw)
        except ValueError:
            msg = f"Invalid {key}: {raw!r} (must be an integer)"
            logger.error(msg)
            sys.exit(1)

        if min_value is not None and value < min_value:
            msg = f"{key} must be >= {min_value}, got {value}"
            logger.error(msg)
            sys.exit(1)

        return value
