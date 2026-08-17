#!/usr/bin/env python3
"""
PTO & Leave Request Agent: Deel identity/policy check -> real balance
validation -> Deel time-off submission -> Google Calendar block -> Slack
manager notification

Runs on behalf of one employee (EMPLOYEE_EMAIL): resolves them to a real
Deel worker, validates the requested leave against their real remaining
balance in Deel, submits a real (pending-approval) time-off request to Deel,
blocks their Google Calendar for the requested dates, and DMs their manager
in Slack. Insufficient balance or invalid dates are rejected BEFORE any
write happens.

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
    build_manager_slack_message,
    build_rejection_message,
    count_business_days,
    parse_entitlement_remaining,
    validate_leave_request,
)
from config import Config
from connectors import (
    Connector,
    ConnectorError,
    DeelConnector,
    GoogleCalendarConnector,
    SlackConnector,
)
import logging_config
from provisioning import ProvisioningError, resolve_employee, resolve_policy, verify_calendar_access
from state import StateManager, compute_request_fingerprint

load_dotenv()
logger = logging_config.setup_logging(__name__)

_shutdown_requested = False


class ShutdownRequested(Exception):
    """Raised to unwind cleanly to exit code 130 when a shutdown signal arrives before any irreversible write (submitting to Deel) has been made."""


def _signal_handler(sig, frame):
    global _shutdown_requested
    logger.warning("Received signal, shutting down gracefully...")
    _shutdown_requested = True


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def init_config() -> Config:
    cfg = Config()
    cfg.validate()
    # Register the exact secret value for redaction before anything else
    # can log it (e.g. a Scalekit client init failure that echoes back its
    # own credentials).
    logging_config.register_secret(cfg.scalekit_client_secret)
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


def run_cycle(cfg: Config, actions, state: StateManager) -> Optional[int]:
    """
    Process the configured leave request once.

    Returns:
      0  if the request was processed successfully (submitted now, or
         already completed on a prior run and correctly skipped)
      2  if the request was rejected by policy validation (insufficient
         balance or invalid dates) -- a business decision, not a system error
      1  if a required step fails in a way that isn't a policy rejection

    Never raises LeaveValidationError or ConnectorError out of this
    function; every failure mode is caught, logged clearly, and turned into
    a return code so the caller (main()) never has to guess what happened.
    """
    deel = DeelConnector(actions, cfg.deel_user, cfg.deel_connector)
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
            f"skipping to avoid a duplicate Deel submission, calendar block, and "
            f"Slack DM. Delete state/processed_requests.json to force a re-run."
        )
        return 0

    if _shutdown_requested:
        raise ShutdownRequested("Shutdown requested before any work started on this request")

    logger.info("Step 1: Resolving employee identity in Deel")
    try:
        person = resolve_employee(deel, cfg.employee_email, cfg.employee_deel_profile_id)
    except ProvisioningError as e:
        logger.error(str(e))
        return 1
    hris_profile_id = person["hris_profile_id"]

    if _shutdown_requested:
        raise ShutdownRequested("Shutdown requested after resolving identity -- no request was created")

    logger.info("Step 2: Resolving assigned time-off policy in Deel")
    deel_policy_type = cfg.deel_policy_type_name()
    try:
        policy = resolve_policy(deel, hris_profile_id, deel_policy_type)
    except ProvisioningError as e:
        logger.error(str(e))
        return 1
    policy_id = policy["id"]

    employee_name = cfg.employee_name or _person_display_name(person) or cfg.employee_email

    if _shutdown_requested:
        raise ShutdownRequested("Shutdown requested after resolving policy -- no request was created")

    logger.info("Step 3: Checking existing overlapping requests in Deel")
    if _has_overlapping_request(deel, cfg.pto_start_date, cfg.pto_end_date, hris_profile_id):
        logger.warning(
            f"An existing REQUESTED or APPROVED time-off request already overlaps "
            f"these dates for this employee in Deel -- skipping submission to avoid "
            f"a duplicate. If this is intentional (e.g. extending a request), submit "
            f"the change directly in Deel."
        )
        state.mark_completed(
            fingerprint,
            status_detail="skipped_existing_deel_request",
            completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        return 0

    if _shutdown_requested:
        raise ShutdownRequested("Shutdown requested after the overlap check -- no request was created")

    logger.info("Step 4: Validating leave request against real Deel balance")
    requested_days = count_business_days(start_date, end_date)
    try:
        entitlement = deel.get_entitlement(hris_profile_id, deel_policy_type)
    except ConnectorError as e:
        logger.error(f"Could not fetch this employee's real balance from Deel: {e}")
        return 1
    if not entitlement:
        logger.error(
            f"Deel returned no entitlement record for this employee's "
            f"'{deel_policy_type}' policy. Cannot validate the requested balance."
        )
        return 1

    try:
        remaining_balance = parse_entitlement_remaining(entitlement)
        remaining_after = validate_leave_request(
            start_date=start_date,
            end_date=end_date,
            requested_days=requested_days,
            remaining_balance=remaining_balance,
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
        f"Balance (per Deel): {remaining_balance:g} -> {remaining_after:g} day(s) remaining"
    )

    if _shutdown_requested:
        raise ShutdownRequested("Shutdown requested before submitting to Deel -- no request was created")

    logger.info("Step 5: Submitting time-off request to Deel")
    try:
        request = deel.create_request(
            recipient_profile_id=hris_profile_id,
            start_date=cfg.pto_start_date,
            end_date=cfg.pto_end_date,
            policy_id=policy_id,
            description=cfg.pto_reason,
        )
    except ConnectorError as e:
        logger.error(f"Failed to submit time-off request to Deel: {e}")
        return 1

    deel_request_id = request.get("id", "")
    logger.info(f"[OK] Deel time-off request submitted (id={deel_request_id}, status=REQUESTED)")
    state.mark_step(
        fingerprint,
        status="deel_submitted",
        employee_email=cfg.employee_email,
        employee_name=employee_name,
        deel_hris_profile_id=hris_profile_id,
        deel_policy_id=policy_id,
        deel_request_id=deel_request_id,
        pto_type=cfg.pto_type,
        start_date=cfg.pto_start_date,
        end_date=cfg.pto_end_date,
        requested_days=requested_days,
        reason=cfg.pto_reason,
    )

    if _shutdown_requested:
        logger.warning(
            "Shutdown requested after submitting to Deel -- the request already exists there; "
            "continuing to record it rather than leaving the calendar/Slack steps undone silently."
        )

    logger.info("Step 6: Blocking Google Calendar for the requested dates")
    calendar_event = None
    calendar_error = None
    existing_out_of_office = _find_overlapping_out_of_office(calendar, cfg.google_calendar_id, start_date, end_date)
    if existing_out_of_office:
        logger.warning(
            f"An existing out-of-office calendar event already overlaps these dates "
            f"(event_id={existing_out_of_office.get('id', '?')}, "
            f"summary={existing_out_of_office.get('summary', '?')!r}) -- skipping "
            f"calendar creation to avoid a duplicate/overlapping block. The Deel "
            f"request and Slack notification for this request still proceed."
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
                f"Failed to block Google Calendar: {e}. Continuing to Step 7 -- "
                f"the Deel request is already submitted; the calendar block can "
                f"be created manually."
            )
            state.mark_step(fingerprint, calendar_error=calendar_error)

    logger.info("Step 7: Notifying manager via Slack")
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

    state.mark_completed(
        fingerprint,
        completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )

    logger.info(
        f"[SUMMARY] Leave request for {employee_name} ({cfg.employee_email}): "
        f"deel_request={deel_request_id or 'FAILED'} (pending approval), "
        f"calendar={'blocked' if calendar_event else f'FAILED ({calendar_error})'}, "
        f"slack={'notified' if manager_user_id else 'skipped (manager unresolvable)'}"
    )
    return 0


def requested_days_span_hours(start_date: datetime.date, end_date: datetime.date) -> int:
    """Total hours spanning the full calendar-day range (used for the outOfOffice block)."""
    span_days = (end_date - start_date).days + 1
    return span_days * 24


def _has_overlapping_request(deel: DeelConnector, start_date: str, end_date: str, hris_profile_id: str) -> bool:
    """
    Check Deel itself for an existing REQUESTED or APPROVED time-off request
    overlapping the given date range for this employee. This is a stronger,
    live check than the local state fingerprint: it also catches a request
    submitted through Deel directly (not via this agent) or by a prior run
    whose local state was lost, not just a byte-for-byte repeat of the same
    CLI invocation.

    Verified live against a real created request: deelmcp_timeoff_request_list's
    response nests the recipient under recipient_profile.hris_profile_id --
    a different shape from deelmcp_timeoff_request_create's flat
    recipient_profile_id input field. Both are checked here in case a
    future response shape flattens it, but the nested field is the one
    confirmed real today.
    """
    try:
        requests = deel.list_requests(start_date, end_date, statuses=["REQUESTED", "APPROVED"])
    except ConnectorError as e:
        logger.warning(f"Could not check Deel for overlapping requests: {e} -- proceeding without this check")
        return False

    for req in requests:
        recipient_profile = req.get("recipient_profile") or {}
        recipient = (
            recipient_profile.get("hris_profile_id")
            or req.get("recipient_profile_id")
            or ""
        )
        if recipient == hris_profile_id:
            return True
    return False


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
    first = person.get("first_name", "") or person.get("firstName", "")
    last = person.get("last_name", "") or person.get("lastName", "")
    name = f"{first} {last}".strip()
    return name


def _notify_manager_of_rejection(slack: SlackConnector, cfg: Config, employee_name: str, reason: str) -> None:
    """Best-effort: let the manager know a request was rejected, without blocking on Slack."""
    manager_user_id = cfg.manager_slack_id or slack.resolve_user_id(cfg.manager_email)
    if not manager_user_id:
        logger.warning("Could not notify manager of rejection (manager not resolvable in Slack)")
        return
    try:
        slack.send_dm(manager_user_id, build_rejection_message(employee_name, reason))
        logger.info(f"[OK] Manager notified of rejection via Slack DM (user_id={manager_user_id})")
    except ConnectorError as e:
        logger.warning(f"Failed to notify manager of rejection: {e}")


def main() -> int:
    cfg = init_config()
    sk = init_scalekit(cfg)
    actions = sk.actions
    state = StateManager()

    logger.info("Step 0: Checking connector auth")
    deel_conn = Connector(actions, cfg.deel_connector, cfg.deel_user)
    deel_active = deel_conn.check_auth()
    other_active = True
    for connector_name, identifier in cfg.get_connector_users():
        if connector_name == cfg.deel_connector and identifier == cfg.deel_user:
            continue  # already checked above
        conn = Connector(actions, connector_name, identifier)
        if not conn.check_auth():
            other_active = False

    if not deel_active:
        # Unlike Google Calendar or Slack (whose failures degrade gracefully
        # later in the pipeline -- see Step 6/7), every step of this agent
        # depends on Deel: there is no leave request to validate, submit, or
        # notify about without it. Fail fast here rather than let the real
        # failure surface a few seconds later at Step 1 with less context.
        logger.error(
            "Deel is not authorized. This agent cannot resolve the employee, "
            "check their balance, or submit a request without it -- fix "
            "authorization before re-running."
        )
        return 1

    if not other_active:
        logger.warning(
            "Google Calendar and/or Slack are not authorized. Proceeding anyway -- "
            "the Deel submission can still succeed; the calendar block and/or "
            "manager notification will be skipped with a warning if their "
            "connector stays unauthorized."
        )

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
                code = run_cycle(cfg, actions, state)
                consecutive_errors = 0
                if code == 0:
                    logger.info("[OK] Request processed (or already complete) -- nothing further to do")
                    return 0
                elif code == 2:
                    logger.info("Request rejected by policy validation -- stopping polling loop")
                    return 2
            except ShutdownRequested as e:
                logger.info(str(e))
                return 130
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
        try:
            return run_cycle(cfg, actions, state)
        except ShutdownRequested as e:
            logger.info(str(e))
            return 130


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.warning("Interrupted by user (signal)")
        sys.exit(130)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)
