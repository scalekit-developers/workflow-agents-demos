#!/usr/bin/env python3
"""
PTO & Leave Request Agent: Gusto identity check -> policy validation ->
Google Calendar block -> Slack manager notification

Runs on behalf of one employee (EMPLOYEE_EMAIL): confirms they are a real
person in Gusto, validates the requested leave against a configured
entitlement (Gusto's connected tools expose no time-off balance/policy
object, see aggregator.py and README), blocks their Google Calendar for the
requested dates, and DMs their manager in Slack. Insufficient balance or
invalid dates are rejected BEFORE any write happens.

Scalekit Agent Auth handles OAuth for all three connectors -- token storage,
refresh, and every API call go through actions.execute_tool(). No manual
token management, no direct API imports.

Setup:
  cp .env.example .env        # fill in your credentials
  pip install -r requirements.txt
  python run_flow.py           # process the configured leave request and exit

Exit codes:
  0   = success (request processed: either submitted, or already completed
        on a prior run and correctly skipped)
  1   = error (config missing, provisioning failed, or 5 consecutive polling
        errors)
  2   = rejected (leave request failed policy validation: insufficient
        balance or invalid dates -- not a system error, a business decision)
  130 = interrupted (Ctrl+C or SIGTERM)
"""

import datetime
import signal
import sys
import time
from typing import Optional

import scalekit.client
from dotenv import load_dotenv

from aggregator import (
    LeaveValidationError,
    build_calendar_summary,
    build_gusto_rejection_message,
    build_manager_slack_message,
    count_business_days,
    validate_leave_request,
)
import config as config_module
from config import Config
from connectors import (
    ConnectorError,
    GoogleCalendarConnector,
    GustoConnector,
    SlackConnector,
)
import logging_config
from provisioning import ProvisioningError, resolve_employee, verify_calendar_access
from state import StateManager, UsageLedger, compute_request_fingerprint

load_dotenv()
logger = logging_config.setup_logging(__name__)
config_module.logger = logger

_shutdown_requested = False


def _signal_handler(sig, frame):
    global _shutdown_requested
    logger.warning("Received signal, shutting down gracefully...")
    _shutdown_requested = True


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def init_config() -> Config:
    cfg = Config()
    cfg.validate()
    return cfg


def init_scalekit(cfg: Config):
    try:
        sk = scalekit.client.ScalekitClient(
            client_id=cfg.scalekit_client_id,
            client_secret=cfg.scalekit_client_secret,
            env_url=cfg.scalekit_env_url,
        )
        logger.debug("Scalekit client initialized")
        return sk
    except Exception as e:
        logger.error(f"Failed to initialize Scalekit: {e}", exc_info=True)
        sys.exit(1)


def run_cycle(cfg: Config, actions, state: StateManager, usage: UsageLedger) -> Optional[int]:
    """
    Process the configured leave request once.

    Returns:
      0  if the request was processed successfully (submitted now, or
         already completed on a prior run and correctly skipped)
      2  if the request was rejected by policy validation (insufficient
         balance or invalid dates) -- a business decision, not a system error
      None only when interrupted mid-cycle by a shutdown signal

    Never raises LeaveValidationError or ConnectorError out of this
    function; every failure mode is caught, logged clearly, and turned into
    a return code so the caller (main()) never has to guess what happened.
    """
    gusto = GustoConnector(actions, cfg.gusto_user, cfg.gusto_connector)
    calendar = GoogleCalendarConnector(actions, cfg.google_calendar_user, cfg.google_calendar_connector)
    slack = SlackConnector(actions, cfg.slack_user, cfg.slack_connector)

    start_date = datetime.date.fromisoformat(cfg.pto_start_date)
    end_date = datetime.date.fromisoformat(cfg.pto_end_date)
    fingerprint = compute_request_fingerprint(cfg.employee_email, cfg.pto_type, cfg.pto_start_date, cfg.pto_end_date)

    if state.is_completed(fingerprint):
        prior = state.get(fingerprint) or {}
        logger.info(
            f"This exact request ({cfg.employee_email}, {cfg.pto_type}, "
            f"{cfg.pto_start_date} to {cfg.pto_end_date}) was already completed "
            f"on a prior run (completed_at={prior.get('completed_at', '?')}) -- "
            f"skipping to avoid a duplicate calendar block and Slack DM. "
            f"Delete state/processed_requests.json to force a re-run."
        )
        return 0

    logger.info("Step 1: Confirming employee identity in Gusto")
    try:
        person = resolve_employee(gusto, cfg.employee_email, cfg.employee_gusto_uuid)
    except ProvisioningError as e:
        logger.error(str(e))
        return 1

    employee_name = cfg.employee_name or _person_display_name(person) or cfg.employee_email

    logger.info("Step 2: Validating leave request against configured policy")
    requested_days = count_business_days(start_date, end_date)
    days_already_used = usage.days_used(cfg.employee_email)

    try:
        remaining_before, remaining_after = validate_leave_request(
            start_date=start_date,
            end_date=end_date,
            requested_days=requested_days,
            annual_entitlement_days=cfg.pto_annual_entitlement_days,
            days_already_used=days_already_used,
        )
    except LeaveValidationError as e:
        logger.error(f"Leave request rejected: {e} [{e.code}]")
        _notify_manager_of_rejection(slack, cfg, employee_name, str(e))
        state.mark_step(
            fingerprint,
            status="rejected",
            reason=str(e),
            reason_code=e.code,
            rejected_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        return 2

    logger.info(
        f"  Requested: {requested_days} business day(s) "
        f"({cfg.pto_start_date} to {cfg.pto_end_date}), type={cfg.pto_type}. "
        f"Balance: {remaining_before:g} -> {remaining_after:g} day(s) remaining "
        f"(entitlement {cfg.pto_annual_entitlement_days:g}, "
        f"already used {days_already_used:g})"
    )

    logger.info(
        "Step 3: Recording the leave request (Gusto has no time-off "
        "submission tool in this workspace, see README -- recorded in "
        "local state instead of a live Gusto write)"
    )
    state.mark_step(
        fingerprint,
        status="pto_recorded",
        employee_email=cfg.employee_email,
        employee_name=employee_name,
        gusto_person_uuid=person.get("uuid", ""),
        gusto_person_type=person.get("gusto_person_type", ""),
        pto_type=cfg.pto_type,
        start_date=cfg.pto_start_date,
        end_date=cfg.pto_end_date,
        requested_days=requested_days,
        reason=cfg.pto_reason,
    )

    logger.info("Step 4: Blocking Google Calendar for the requested dates")
    calendar_event = None
    calendar_error = None
    existing_out_of_office = _find_overlapping_out_of_office(calendar, cfg.google_calendar_id, start_date, end_date)
    if existing_out_of_office:
        logger.warning(
            f"An existing out-of-office calendar event already overlaps these dates "
            f"(event_id={existing_out_of_office.get('id', '?')}, "
            f"summary={existing_out_of_office.get('summary', '?')!r}) -- skipping "
            f"calendar creation to avoid a duplicate/overlapping block. The Gusto "
            f"record and Slack notification for this request still proceed."
        )
        calendar_event = existing_out_of_office
        state.mark_step(fingerprint, calendar_event_id=existing_out_of_office.get("id", ""), calendar_reused_existing=True)
    else:
        try:
            summary = build_calendar_summary(employee_name, cfg.pto_type)
            duration_hours = requested_days_span_hours(start_date, end_date)
            result = calendar.create_out_of_office_block(
                calendar_id=cfg.google_calendar_id,
                summary=summary,
                start_datetime=f"{start_date.isoformat()}T00:00:00Z",
                duration_hours=duration_hours,
            )
            calendar_event = result.get("event") or {}
            event_id = calendar_event.get("id", "")
            html_link = calendar_event.get("htmlLink", "")
            logger.info(f"[OK] Calendar blocked: event_id={event_id}")
            state.mark_step(
                fingerprint,
                calendar_event_id=event_id,
                calendar_html_link=html_link,
                calendar_blocked_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            )
        except ConnectorError as e:
            calendar_error = str(e)
            logger.warning(
                f"Failed to block Google Calendar: {e}. Continuing to Step 5 -- "
                f"the leave request itself is still recorded; the calendar block "
                f"can be created manually."
            )
            state.mark_step(fingerprint, calendar_error=calendar_error)

    logger.info("Step 5: Notifying manager via Slack")
    manager_user_id = cfg.manager_slack_id or slack.resolve_user_id(cfg.manager_email)
    if not manager_user_id:
        logger.warning(
            f"Could not resolve manager '{cfg.manager_email}' to a Slack user -- "
            f"skipping Slack notification. Set MANAGER_SLACK_ID directly, or "
            f"confirm the manager's email matches their Slack profile email."
        )
        state.mark_step(fingerprint, slack_skipped_reason="manager_not_resolvable")
    else:
        message = build_manager_slack_message(
            employee_name=employee_name,
            employee_email=cfg.employee_email,
            pto_type=cfg.pto_type,
            start_date=start_date,
            end_date=end_date,
            requested_days=requested_days,
            remaining_after=remaining_after,
            reason=cfg.pto_reason,
            calendar_event_created=calendar_event is not None,
            calendar_html_link=(calendar_event or {}).get("htmlLink", ""),
        )
        try:
            slack.send_dm(manager_user_id, message)
            logger.info(f"[OK] Manager notified via Slack DM (user_id={manager_user_id})")
            state.mark_step(
                fingerprint,
                slack_notified_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                manager_slack_id=manager_user_id,
            )
        except ConnectorError as e:
            logger.warning(f"Failed to send Slack DM to manager: {e}")
            state.mark_step(fingerprint, slack_error=str(e))

    new_total_used = usage.add_days(cfg.employee_email, requested_days)
    logger.debug(f"Updated PTO usage ledger for {cfg.employee_email}: {new_total_used:g} day(s) used this year")

    state.mark_completed(
        fingerprint,
        completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )

    logger.info(
        f"[SUMMARY] Leave request for {employee_name} ({cfg.employee_email}): "
        f"policy=OK, gusto_record={'recorded (no live Gusto write tool available)'}, "
        f"calendar={'blocked' if calendar_event else f'FAILED ({calendar_error})'}, "
        f"slack={'notified' if manager_user_id else 'skipped (manager unresolvable)'}"
    )
    return 0


def requested_days_span_hours(start_date: datetime.date, end_date: datetime.date) -> int:
    """Total hours spanning the full calendar-day range (used for the outOfOffice block)."""
    span_days = (end_date - start_date).days + 1
    return span_days * 24


def _find_overlapping_out_of_office(
    calendar: GoogleCalendarConnector, calendar_id: str, start_date: datetime.date, end_date: datetime.date
) -> Optional[dict]:
    """
    Check for an existing "outOfOffice" event that already overlaps the
    requested date range, so re-running the agent for the same (or an
    overlapping) leave period doesn't create a second, duplicate calendar
    block. This is a separate guard from state.py's exact-match idempotency
    fingerprint: it also catches a genuinely new request whose dates happen
    to overlap a previously created block (e.g. extending a leave request by
    a day), not just a byte-for-byte repeat of the same request.
    """
    time_min = f"{start_date.isoformat()}T00:00:00Z"
    time_max = f"{(end_date + datetime.timedelta(days=1)).isoformat()}T00:00:00Z"
    try:
        events = calendar.list_events(calendar_id, time_min, time_max)
    except ConnectorError as e:
        logger.warning(f"Could not check for overlapping calendar events: {e} -- proceeding without this check")
        return None

    for event in events:
        if event.get("eventType") == "outOfOffice":
            return event
    return None


def _person_display_name(person: dict) -> str:
    first = person.get("first_name", "")
    last = person.get("last_name", "")
    name = f"{first} {last}".strip()
    return name


def _notify_manager_of_rejection(slack: SlackConnector, cfg: Config, employee_name: str, reason: str) -> None:
    """Best-effort: let the manager know a request was rejected, without blocking on Slack."""
    manager_user_id = cfg.manager_slack_id or slack.resolve_user_id(cfg.manager_email)
    if not manager_user_id:
        logger.warning("Could not notify manager of rejection (manager not resolvable in Slack)")
        return
    try:
        slack.send_dm(manager_user_id, build_gusto_rejection_message(employee_name, reason))
        logger.info(f"[OK] Manager notified of rejection via Slack DM (user_id={manager_user_id})")
    except ConnectorError as e:
        logger.warning(f"Failed to notify manager of rejection: {e}")


def main() -> int:
    cfg = init_config()
    sk = init_scalekit(cfg)
    actions = sk.actions
    state = StateManager()
    usage = UsageLedger()

    logger.info("Step 0: Checking connector auth")
    all_active = True
    for connector_name, identifier in cfg.get_connector_users().items():
        conn = _connector_for_check(actions, connector_name, identifier)
        if not conn.check_auth():
            all_active = False

    if not all_active:
        logger.warning("Some connectors are not authorized. Proceeding anyway -- affected steps will be skipped.")

    logger.info("Step 0.5: Verifying Google Calendar destination is reachable")
    calendar = GoogleCalendarConnector(actions, cfg.google_calendar_user, cfg.google_calendar_connector)
    try:
        verify_calendar_access(calendar, cfg.google_calendar_id)
    except ProvisioningError as e:
        logger.error(str(e))
        return 1

    if cfg.polling_mode:
        logger.info(
            f"Polling mode enabled (interval: {cfg.poll_interval_minutes}m, press Ctrl+C to stop). "
            f"Since a leave request is a one-shot action once submitted, polling here re-checks "
            f"whether the CONFIGURED request has been fully processed yet -- it does not resubmit "
            f"work that already completed (see state.py)."
        )
        consecutive_errors = 0
        cycle = 0

        while True:
            if _shutdown_requested:
                logger.info("Graceful shutdown")
                return 130

            cycle += 1
            logger.info(f"Polling cycle #{cycle}")

            try:
                code = run_cycle(cfg, actions, state, usage)
                consecutive_errors = 0
                if code == 0:
                    logger.info("[OK] Request processed (or already complete) -- nothing further to do")
                    return 0
                elif code == 2:
                    logger.info("Request rejected by policy validation -- stopping polling loop")
                    return 2
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Error during cycle: {e}", exc_info=True)
                if consecutive_errors >= 5:
                    logger.critical("5 consecutive errors, exiting")
                    return 1

            for _ in range(cfg.poll_interval_minutes * 60):
                if _shutdown_requested:
                    logger.info("Graceful shutdown")
                    return 130
                time.sleep(1)

    else:
        return run_cycle(cfg, actions, state, usage)


def _connector_for_check(actions, connector_name: str, identifier: str):
    """Lightweight wrapper just for the Step 0 auth-check loop."""
    from connectors import Connector
    return Connector(actions, connector_name, identifier)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.warning("Interrupted by user (signal)")
        sys.exit(130)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)
