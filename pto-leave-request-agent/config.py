"""
Configuration management with validation.

All settings loaded from environment variables.
Validates on startup and provides clear error messages.
"""

import datetime
import os
import sys
from typing import Dict, Optional

logger = None  # Set by run_flow after logging is initialized

# Gusto's real policy-type taxonomy, verified against the workspace's Gusto
# company profile (gustomcp_get_company -> compensations.hourly), which lists
# time-off-adjacent earning types such as "Outstanding vacation", "Holiday",
# "Emergency sick - self care", "Emergency sick - caring for others", and
# "FMLA Public Health Emergency Leave". There is no dedicated time-off policy
# object exposed by GUSTOMCP's tool surface (see connectors.py and README),
# so this agent normalizes PTO_TYPE to one of a small, human-readable set
# rather than trying to match Gusto's earning-type UUIDs exactly.
VALID_PTO_TYPES = ("vacation", "sick", "personal", "bereavement", "other")


class Config:
    """Application configuration."""

    def __init__(self):
        """Load configuration from environment variables."""
        # Scalekit
        self.scalekit_env_url = os.environ.get("SCALEKIT_ENV_URL")
        self.scalekit_client_id = os.environ.get("SCALEKIT_CLIENT_ID")
        self.scalekit_client_secret = os.environ.get("SCALEKIT_CLIENT_SECRET")

        # Connector identities (the "identifier" each connected account is keyed by)
        self.gusto_user = os.environ.get("GUSTO_USER")
        self.google_calendar_user = os.environ.get("GOOGLE_CALENDAR_USER")
        self.slack_user = os.environ.get("SLACK_USER")

        # Connector names -- must match the exact connection name shown in the
        # Scalekit dashboard, which is often auto-suffixed per workspace
        # (e.g. "gustomcp-SoSOMZ20"), not just the generic provider label.
        # Verified live against this workspace's connections via
        # list_connected_accounts before writing connectors.py.
        self.gusto_connector = os.environ.get("GUSTO_CONNECTOR", "gustomcp-SoSOMZ20")
        self.google_calendar_connector = os.environ.get("GOOGLE_CALENDAR_CONNECTOR", "googlecalendar")
        self.slack_connector = os.environ.get("SLACK_CONNECTOR", "slackmcp")

        # The employee whose PTO this run is for (the delegated identity this
        # agent acts on behalf of). Gusto's connected tool surface has no
        # time-off-specific tools (see connectors.py module docstring), so
        # this is used to look the person up as a Gusto employee OR
        # contractor record (gustomcp_list_employees / gustomcp_list_contractors)
        # purely for identity verification in provisioning.py, not for
        # balance/policy data.
        self.employee_email = os.environ.get("EMPLOYEE_EMAIL")
        self.employee_name = os.environ.get("EMPLOYEE_NAME", "")

        # Optional: if you already know the employee's Gusto UUID, set it to
        # skip the list-and-match lookup in provisioning.py.
        self.employee_gusto_uuid = os.environ.get("EMPLOYEE_GUSTO_UUID", "")

        # Manager notified via Slack DM. Resolved to a Slack user ID via
        # slackmcp_slack_search_users if MANAGER_SLACK_ID isn't set directly.
        self.manager_email = os.environ.get("MANAGER_EMAIL")
        self.manager_slack_id = os.environ.get("MANAGER_SLACK_ID", "")

        # The leave request itself.
        self.pto_start_date = os.environ.get("PTO_START_DATE", "")
        self.pto_end_date = os.environ.get("PTO_END_DATE", "")
        self.pto_type = os.environ.get("PTO_TYPE", "vacation").strip().lower()
        self.pto_reason = os.environ.get("PTO_REASON", "")

        # Annual leave entitlement and already-used days, in whole days, used
        # as the balance/policy source of truth. Gusto's MCP connector in
        # this workspace exposes no time-off-balance or time-off-policy tool
        # (verified live across its full 38-tool catalog, see connectors.py),
        # so this agent cannot read a real balance from Gusto and instead
        # validates against a configured entitlement. This is a documented
        # workaround, not a silent guess -- see README's Error Handling
        # section for the full explanation.
        self.pto_annual_entitlement_days = self._parse_float(
            "PTO_ANNUAL_ENTITLEMENT_DAYS", 20.0, min_value=0.0
        )

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

        if not self.gusto_user:
            errors.append("GUSTO_USER")
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
            if logger:
                logger.error(msg)
            else:
                print(f"ERROR: {msg}")
            sys.exit(1)

        if self.pto_type not in VALID_PTO_TYPES:
            msg = (
                f"Invalid PTO_TYPE: {self.pto_type!r}. Must be one of: "
                f"{', '.join(VALID_PTO_TYPES)}"
            )
            if logger:
                logger.error(msg)
            else:
                print(f"ERROR: {msg}")
            sys.exit(1)

        self._validate_dates()

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
        if logger:
            logger.error(msg)
        else:
            print(f"ERROR: {msg}")
        sys.exit(1)

    def get_connector_users(self) -> Dict[str, str]:
        """Mapping of connector name -> identifier, for auth checks."""
        return {
            self.gusto_connector: self.gusto_user,
            self.google_calendar_connector: self.google_calendar_user,
            self.slack_connector: self.slack_user,
        }

    @staticmethod
    def _parse_int(key: str, default: int, min_value: int = None) -> int:
        raw = os.environ.get(key, str(default))
        try:
            value = int(raw)
        except ValueError:
            msg = f"Invalid {key}: {raw!r} (must be an integer)"
            if logger:
                logger.error(msg)
            else:
                print(f"ERROR: {msg}")
            sys.exit(1)

        if min_value is not None and value < min_value:
            msg = f"{key} must be >= {min_value}, got {value}"
            if logger:
                logger.error(msg)
            else:
                print(f"ERROR: {msg}")
            sys.exit(1)

        return value

    @staticmethod
    def _parse_float(key: str, default: float, min_value: float = None) -> float:
        raw = os.environ.get(key, str(default))
        try:
            value = float(raw)
        except ValueError:
            msg = f"Invalid {key}: {raw!r} (must be a number)"
            if logger:
                logger.error(msg)
            else:
                print(f"ERROR: {msg}")
            sys.exit(1)

        if min_value is not None and value < min_value:
            msg = f"{key} must be >= {min_value}, got {value}"
            if logger:
                logger.error(msg)
            else:
                print(f"ERROR: {msg}")
            sys.exit(1)

        return value
