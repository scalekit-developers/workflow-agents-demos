#!/usr/bin/env python3
"""
Payroll Change Request Agent: Gusto -> Slack + Google Sheets

Runs on behalf of an employee to submit a payroll or bank/direct-deposit
detail change. Verifies the employee's record and change eligibility in
Gusto (a hard gate -- see Step 1), submits the change as that employee,
logs the change to Google Sheets (with the sensitive new value masked), and
sends a confirmation Slack DM to the employee (also masked).

This agent handles some of the most sensitive data any agent in this
workspace touches: bank account numbers, routing numbers, and payroll
details. See README.md's "Data Handling & Security" section for the full
policy. In short: the raw new value is NEVER logged, NEVER written to
Google Sheets, and NEVER included in the Slack confirmation -- only a
masked form (last 4 characters) ever leaves memory, and logging_config.py
additionally redacts anything that merely LOOKS like a bank/routing number
as a defense-in-depth backstop.

IMPORTANT, verified live against this workspace's Scalekit environment: the
GustoMCP connector currently exposes ONLY read tools (list/get employees,
contractors, compensations, etc; every tool's read_only_hint is true). There
is no create/update/delete tool for employee/contractor bank details,
payment methods, or compensation. GustoConnector.submit_payroll_change() is
fully implemented per Gusto's real write-endpoint shape but raises
GustoWriteNotAvailableError when called, rather than guessing at a
nonexistent tool name. Step 2 below handles this explicitly: it is not a
silently-skipped step, it is a loud, distinct, correctly-exit-coded outcome
(see Exit codes below), unless SIMULATE_SUBMISSION=true is set, in which
case Step 2 runs a clearly-labeled simulated write so the rest of the
pipeline (idempotency, masking, Sheets logging, Slack confirmation) can be
proven correct end-to-end without ever touching a real Gusto account.

Scalekit Agent Auth handles OAuth for all three connectors -- token storage,
refresh, and every API call go through actions.execute_tool(). No manual
token management, no direct API imports.

Setup:
  cp .env.example .env        # fill in your credentials
  pip install -r requirements.txt
  python run_flow.py           # process one payroll-change request and exit

Exit codes:
  0   = success (change submitted or simulated, eligibility passed)
  1   = error (config missing, provisioning failed, or unexpected exception)
  2   = no data (employee not found in Gusto at all)
  3   = eligibility gate failed or was inconclusive -- refused to submit.
        This is distinct from exit code 2 ("no data") deliberately: 2 means
        "there was nothing to evaluate at all" (the employee record itself
        does not exist), while 3 means "the employee record exists, but this
        specific change was correctly and deliberately refused" -- these are
        different operational situations. A monitoring/alerting setup should
        treat 3 as expected-and-frequent (people ops staff will trigger this
        regularly for employees who are on leave, mid-termination, etc, and
        it is NOT a bug or an infrastructure problem), while 1 and 2 usually
        indicate a real setup problem worth paging someone about. Folding the
        eligibility refusal into exit code 2 (as a lazier design might) would
        make it impossible for a caller to tell "there's no employee record"
        apart from "there's a record but we correctly said no" from the exit
        code alone, which defeats the purpose of a hard gate that must be
        LOUD and DISTINCT, not just another flavor of "no data".
  4   = Gusto rejected the submission (a policy reason on Gusto's side, not
        caught by this agent's own eligibility check) OR no Gusto write
        tool is available in this Scalekit environment and
        SIMULATE_SUBMISSION was not set to true. Distinct from exit code 3:
        3 means "we refused to even try"; 4 means "we tried (or would have
        tried, and could not) and the attempt itself did not succeed".
  130 = interrupted (Ctrl+C or SIGTERM)

Note on Sheets-logging and Slack-notification failures: these do NOT change
the exit code away from 0 if the actual Gusto submission/simulation
succeeded. A failed audit-log write or a failed confirmation DM is an
operational issue that gets logged loudly (ERROR for Sheets, WARNING for
Slack -- see Step 3/Step 4 below and README's Error Handling section) but is
never conflated with "did the payroll change itself succeed or fail". The
final summary line always reports the payroll-change outcome and the
notification outcomes as separate facts.
"""

import datetime
import signal
import sys
from typing import Optional

import scalekit.client
from dotenv import load_dotenv

from aggregator import (
    build_sheets_row,
    build_slack_confirmation,
    check_employee_eligibility,
    describe_change,
    mask_value,
    validate_new_value,
)
import config as config_module
from config import Config
from connectors import (
    ConnectorError,
    GoogleSheetsConnector,
    GustoConnector,
    GustoWriteNotAvailableError,
    SlackConnector,
)
import logging_config
from provisioning import ProvisioningError, ensure_google_sheet_tab, find_employee_record
from state import StateManager, compute_change_fingerprint

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


def run_cycle(cfg: Config, actions, state: StateManager) -> int:
    """
    Run one full payroll-change-request cycle. Returns the exit code that
    main() should propagate (0/2/3/4 -- see module docstring for what each
    means). Step 1's eligibility gate is enforced here as a hard stop: if it
    fails, the function returns immediately with exit code 3 and does NOT
    proceed to Step 2 under any circumstances.
    """
    gusto = GustoConnector(actions, cfg.gusto_user, cfg.gusto_connector)
    slack = SlackConnector(actions, cfg.slack_user, cfg.slack_connector)
    sheets = GoogleSheetsConnector(actions, cfg.google_sheets_user, cfg.google_sheets_connector)

    # --- Step 1: verify employee record and change eligibility in Gusto (HARD GATE) ---
    logger.info(f"Step 1: Verifying employee record and change eligibility for '{cfg.employee_email}'")

    if cfg.employee_gusto_id:
        record_type = cfg.employee_record_type or "employee"
        try:
            record = (
                gusto.get_contractor_detail(cfg.employee_gusto_id)
                if record_type == "contractor"
                else gusto.get_employee_detail(cfg.employee_gusto_id)
            )
        except ConnectorError as e:
            logger.error(f"Could not fetch Gusto record for EMPLOYEE_GUSTO_ID '{cfg.employee_gusto_id}': {e}")
            record = None
    else:
        try:
            record, record_type = find_employee_record(gusto, cfg.employee_email, cfg.employee_record_type)
        except ProvisioningError as e:
            logger.error(str(e))
            return 1

    if record is None:
        logger.error(
            f"EMPLOYEE NOT FOUND: no Gusto employee or contractor record matches "
            f"'{cfg.employee_email}'. Cannot proceed -- refusing to submit any change."
        )
        return 2

    eligibility = check_employee_eligibility(record, record_type)
    value_check = validate_new_value(cfg.change_type, cfg.new_value)

    if not eligibility.eligible or not value_check.eligible:
        all_reasons = eligibility.reasons + value_check.reasons
        logger.error("=" * 70)
        logger.error("ELIGIBILITY GATE FAILED -- REFUSING TO SUBMIT PAYROLL CHANGE")
        for reason in all_reasons:
            logger.error(f"  - {reason}")
        logger.error("=" * 70)
        logger.error(
            "This is a hard stop: no Gusto submission, no Sheets log entry, and no "
            "Slack confirmation will be sent for this request."
        )
        return 3

    logger.info(
        f"[OK] Eligibility gate passed for {record_type} '{cfg.employee_email}' "
        f"(record {eligibility.record_uuid})"
    )

    # --- Idempotency check: exact-duplicate resubmission guard ---
    fingerprint = compute_change_fingerprint(cfg.employee_email, cfg.change_type, cfg.new_value)
    existing = state.get_record(fingerprint)
    if existing is not None:
        logger.warning(
            f"DUPLICATE SUBMISSION DETECTED: an identical change ({cfg.change_type}, "
            f"masked value {existing.get('masked_value')}) for '{cfg.employee_email}' was "
            f"already processed at {existing.get('submitted_at')}. Not resubmitting to Gusto, "
            f"not re-logging to Sheets, not re-sending a Slack confirmation."
        )
        return 0

    masked_value = mask_value(cfg.change_type, cfg.new_value)
    run_date = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    # --- Step 2: submit the payroll/bank detail change as the employee ---
    logger.info(f"Step 2: Submitting {cfg.change_type} change for '{cfg.employee_email}' (masked: ...{masked_value[-4:]})")

    gusto_status = "not_attempted"
    gusto_detail = ""
    submission_succeeded = False

    if cfg.simulate_submission:
        logger.warning(
            "SIMULATED SUBMISSION (SIMULATE_SUBMISSION=true): NOT calling any real Gusto "
            "write tool. This exercises the surrounding pipeline logic only (masking, "
            "idempotency, Sheets logging, Slack confirmation) against no live Gusto write."
        )
        gusto_status = "simulated"
        gusto_detail = "Simulated submission, no real Gusto write tool was called"
        submission_succeeded = True
    else:
        try:
            gusto.submit_payroll_change(
                record_uuid=eligibility.record_uuid,
                record_type=record_type,
                change_type=cfg.change_type,
                new_value=cfg.new_value,
            )
            gusto_status = "submitted"
            submission_succeeded = True
        except GustoWriteNotAvailableError as e:
            logger.error(f"GUSTO SUBMISSION NOT POSSIBLE: {e}")
            gusto_status = "write_tool_unavailable"
            gusto_detail = "No Gusto write tool exposed in this Scalekit environment"
        except ConnectorError as e:
            logger.error(f"GUSTO REJECTED THE SUBMISSION: {e}")
            gusto_status = "rejected"
            gusto_detail = str(e)

    if not submission_succeeded:
        logger.error(
            "Payroll change did NOT succeed. Not logging a success row to Sheets, "
            "not sending a success Slack confirmation. See error above for the reason."
        )
        return 4

    logger.info(f"[OK] Payroll change {'simulated' if cfg.simulate_submission else 'submitted'} successfully")
    state.mark_processed(fingerprint, cfg.employee_email, cfg.change_type, masked_value, run_date)

    # --- Step 3: log the change to Google Sheets (masked) ---
    logger.info("Step 3: Logging the change to Google Sheets (masked value only)")
    detail_text = describe_change(cfg.change_type, masked_value) + (
        " [SIMULATED]" if cfg.simulate_submission else ""
    )
    row = build_sheets_row(
        run_date=run_date,
        employee_email=cfg.employee_email,
        change_type=cfg.change_type,
        masked_value=masked_value,
        status="simulated" if cfg.simulate_submission else "submitted",
        detail=detail_text,
    )
    try:
        sheets.append_row(cfg.google_sheets_spreadsheet_id, cfg.google_sheets_tab_name, row)
        logger.info("[OK] Audit log row written to Google Sheets")
    except ConnectorError as e:
        logger.error(
            f"AUDIT LOG WRITE FAILED after a SUCCESSFUL payroll change: {e}. "
            f"The payroll change itself succeeded and is NOT lost or reversed, but this "
            f"change is now missing from the Google Sheets audit trail -- "
            f"someone must add it manually. This is an operational gap, not a data-loss "
            f"event for the payroll change itself."
        )

    # --- Step 4: send confirmation Slack DM to the employee ---
    logger.info("Step 4: Sending confirmation Slack DM to the employee")
    employee_name = cfg.employee_email.split("@")[0]
    slack_user_id = cfg.employee_slack_id or None
    try:
        if not slack_user_id:
            slack_user_id = slack.resolve_user_id_by_email(cfg.employee_email)
        if not slack_user_id:
            logger.warning(
                f"Could not resolve a Slack user ID for '{cfg.employee_email}' -- skipping "
                f"confirmation DM. The payroll change itself already succeeded; this only "
                f"affects the employee's notification."
            )
        else:
            text = build_slack_confirmation(employee_name, cfg.change_type, masked_value, run_date)
            if cfg.simulate_submission:
                text += "\n\n(Note: this was a SIMULATED change for pipeline testing.)"
            slack.send_dm(slack_user_id, text)
            logger.info(f"[OK] Confirmation Slack DM sent to {slack_user_id}")
    except ConnectorError as e:
        logger.warning(
            f"Failed to send Slack confirmation: {e}. The payroll change and audit log "
            f"already succeeded; only the employee notification failed."
        )

    logger.info(
        f"[SUMMARY] Payroll change: {'SIMULATED' if cfg.simulate_submission else 'SUCCEEDED'} | "
        f"Sheets audit log: see above | Slack notification: see above"
    )
    return 0


def main() -> int:
    cfg = init_config()
    sk = init_scalekit(cfg)
    actions = sk.actions
    state = StateManager()

    logger.info("Step 0: Checking connector auth")
    all_active = True
    for connector_name, identifier in cfg.get_connector_users().items():
        conn = _connector_for_check(actions, connector_name, identifier)
        if not conn.check_auth():
            all_active = False

    if not all_active:
        logger.warning("Some connectors are not authorized. Proceeding anyway -- affected steps will be skipped.")

    logger.info("Step 0.5: Verifying Google Sheets destination")
    sheets = GoogleSheetsConnector(actions, cfg.google_sheets_user, cfg.google_sheets_connector)
    try:
        ensure_google_sheet_tab(sheets, cfg.google_sheets_spreadsheet_id, cfg.google_sheets_tab_name)
    except ProvisioningError as e:
        logger.error(str(e))
        return 1

    if _shutdown_requested:
        logger.info("Graceful shutdown before processing began")
        return 130

    return run_cycle(cfg, actions, state)


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
