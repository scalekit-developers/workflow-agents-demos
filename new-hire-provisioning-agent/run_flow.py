#!/usr/bin/env python3
"""
New Hire Provisioning Agent: Deel -> Google Workspace + Notion + Slack

Runs on behalf of an HR admin, in one of two modes (NEW_HIRE_MODE):

  create (default) -- creates a real direct-employee record for one new hire
    in Deel (person + employment contract, under the organization's own
    legal entity) from NEW_HIRE_* env vars, then provisions Google Workspace
    + Notion + Slack for them. Deel has no delete/terminate tool for a
    direct employee anywhere in its real catalog (confirmed live -- see
    connectors.py), so a mistaken real creation cannot be undone through
    this agent or any other Scalekit tool. NEW_HIRE_DRY_RUN=true resolves
    every real ID (legal entity, team, seniority) and logs exactly what
    WOULD be created without ever calling the real creation tool -- strongly
    recommended for first-time setup verification.

  scan -- detects hires FROM Deel instead, via
    deelmcp_onboarding_tracker_list (see connectors.py
    DeelConnector.list_onboarding_hires and hire.py). This agent never
    creates a Deel record in this mode -- an HR admin already created the
    hire directly in Deel (or a separate create-mode run did). Every hire
    currently at progress.status "INVITED" that isn't already fully
    provisioned gets Workspace + Notion + Slack run for them. This mode
    exists because an earlier build of this agent incorrectly concluded
    Deel had no way to detect existing hires -- deelmcp_onboarding_tracker_list
    genuinely does this, confirmed live (see connectors.py for the full
    correction, including a real quirk where the tool's own progressStatuses
    filter param does not reliably return INVITED records).

Google Workspace provisioning is optional/conditional in both modes: if the
GOOGLEDWD connector isn't configured (a common state until an HR admin
completes the GCP service account + Domain-Wide Delegation setup, see
README), this step logs a clear actionable warning and the pipeline
continues to Notion and Slack rather than aborting. The final summary always
states each step's real outcome (OK / SKIPPED / FAILED) rather than a single
pass/fail flag.

Re-running is a safe no-op in both modes: state.py tracks which steps have
already succeeded per hire (fingerprinted by name+email+date in create mode,
by Deel's own contract ID in scan mode -- see state.py), so a retry never
creates a duplicate Deel record, Notion page, or Slack post.

Scalekit Agent Auth handles OAuth for all connectors -- token storage,
refresh, and every API call go through actions.execute_tool(). No manual
token management, no direct API imports.

Setup:
  cp .env.example .env        # fill in your credentials
  pip install -r requirements.txt
  python run_flow.py           # provision (create mode) or scan and
                                # provision (scan mode) and exit

Exit codes:
  0   = success (all applicable hires fully processed, or already completed
        on a prior run and correctly skipped; in scan mode, zero matching
        hires found is also success)
  1   = error (config missing, provisioning failed, Deel unreachable, or 5
        consecutive polling errors)
  2   = create mode only: the Deel creation itself failed -- not a system
        error in the sense of a bug, but the one outcome serious enough not
        to fold into a generic "0 with warnings" the way Workspace/Notion/
        Slack failures do
  130 = interrupted (Ctrl+C or SIGTERM)
"""

import signal
import sys
import time
from typing import List, Optional

import scalekit.client
from dotenv import load_dotenv

from aggregator import (
    build_onboarding_page_markdown,
    build_onboarding_page_title,
    build_welcome_message,
)
from config import Config
from connectors import (
    Connector,
    ConnectorError,
    ConnectorUnavailableError,
    DeelConnector,
    GoogleWorkspaceConnector,
    NotionConnector,
    SlackConnector,
)
from hire import Hire, hire_from_config, hire_from_tracker_record
import logging_config
from provisioning import (
    ProvisioningError,
    resolve_deel_legal_entity,
    resolve_deel_team,
    verify_deel_writable,
    verify_notion_parent_page,
)
from state import STEP_DEEL, STEP_NOTION, STEP_SLACK, STEP_WORKSPACE, StateManager, compute_hire_fingerprint, compute_scan_fingerprint

load_dotenv()
logger = logging_config.setup_logging(__name__)

_shutdown_requested = False


class ShutdownRequested(Exception):
    """Raised to unwind cleanly to exit code 130 when a shutdown signal arrives before the irreversible Deel creation has been made."""


def _signal_handler(sig, frame):
    global _shutdown_requested
    logger.warning("Received signal, shutting down gracefully...")
    _shutdown_requested = True


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def init_config() -> Config:
    cfg = Config()
    cfg.validate()
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


def provision_hire(
    cfg: Config,
    hire: Hire,
    fingerprint: str,
    state: StateManager,
    workspace: GoogleWorkspaceConnector,
    notion: NotionConnector,
    slack: SlackConnector,
    deel_status: str,
    deel_contract_id: str,
) -> None:
    """
    Run the shared Workspace -> Notion -> Slack steps for one hire, whatever
    mode produced it. deel_status/deel_contract_id describe what already
    happened on the Deel side (a real creation in create mode, or "already
    exists" in scan mode) and are only used for the Notion doc's content --
    this function never touches Deel itself.
    """
    workspace_status = "Not provisioned: Google Workspace step not attempted"
    if state.is_step_done(fingerprint, STEP_WORKSPACE):
        workspace_status = "Already provisioned (see prior run)"
        logger.info(f"Google Workspace account already provisioned for {hire.full_name}, skipping")
    elif not cfg.google_workspace_domain:
        workspace_status = "Not provisioned: GOOGLE_WORKSPACE_DOMAIN not configured"
        logger.warning("GOOGLE_WORKSPACE_DOMAIN not set, skipping Google Workspace provisioning")
    else:
        logger.info(f"Provisioning Google Workspace account via Domain-Wide Delegation for {hire.full_name}")
        try:
            local_part = hire.full_name.strip().lower().replace(" ", ".") or "new.hire"
            primary_email = f"{local_part}@{cfg.google_workspace_domain}"
            workspace.provision_user(
                primary_email=primary_email,
                first_name=hire.first_name,
                last_name=hire.last_name,
            )
            workspace_status = f"Provisioned: {primary_email}"
            state.mark_step_done(fingerprint, STEP_WORKSPACE, {"email": primary_email})
            logger.info(f"[OK] Google Workspace account provisioned: {primary_email}")
        except ConnectorUnavailableError as e:
            workspace_status = "Not provisioned: Google Workspace connector not configured"
            logger.warning(f"Google Workspace connector is not configured: {e}. Continuing to Notion and Slack.")
        except ConnectorError as e:
            workspace_status = f"FAILED: {e}"
            logger.error(f"Google Workspace provisioning failed: {e}. Continuing to Notion and Slack.")

    if state.is_step_done(fingerprint, STEP_NOTION):
        logger.info(f"Notion onboarding page already created for {hire.full_name}, skipping")
    else:
        logger.info(f"Creating Notion onboarding page for {hire.full_name}")
        title = build_onboarding_page_title(hire)
        markdown_body = build_onboarding_page_markdown(hire, workspace_status, deel_status, deel_contract_id)
        try:
            result = notion.upsert_onboarding_page(cfg.notion_parent_page_id, title, markdown_body)
            page_id = ((result.get("pages") or [{}])[0]).get("id", "")
            state.mark_step_done(fingerprint, STEP_NOTION, {"page_id": page_id})
            logger.info(f"[OK] Notion onboarding page ready: {page_id}")
        except ConnectorError as e:
            logger.error(f"Failed to create Notion onboarding page: {e}. Continuing to Slack.")

    if state.is_step_done(fingerprint, STEP_SLACK):
        logger.info(f"Slack welcome message already posted for {hire.full_name}, skipping")
    else:
        logger.info(f"Posting welcome message to Slack for {hire.full_name}")
        channel_id = slack.resolve_channel_id(cfg.slack_welcome_channel)
        if not channel_id:
            logger.error(
                f"Could not resolve Slack channel '{cfg.slack_welcome_channel}'. "
                f"Confirm the channel exists and the bot is a member, or set "
                f"SLACK_WELCOME_CHANNEL to a literal channel ID."
            )
        else:
            message = build_welcome_message(hire)
            try:
                slack.send_welcome_message(channel_id, message)
                state.mark_step_done(fingerprint, STEP_SLACK, {"channel_id": channel_id})
                logger.info(f"[OK] Welcome message posted to Slack ({cfg.slack_welcome_channel} -> {channel_id})")
            except ConnectorError as e:
                logger.error(f"Failed to post Slack welcome message: {e}")


def run_create_cycle(cfg: Config, actions, state: StateManager) -> int:
    """
    Create mode: create the one NEW_HIRE_*-configured hire in Deel (unless
    already done or dry-run), then run Workspace/Notion/Slack for them.

    Returns:
      0  the hire was fully processed (created now, or already completed on
         a prior run and correctly skipped)
      2  the Deel creation itself failed or was rejected
      1  a required step (config/provisioning) fails before reaching Deel

    Never raises ConnectorError out of this function; every failure mode is
    caught, logged clearly, and turned into a return code.
    """
    deel = DeelConnector(actions, cfg.deel_user, cfg.deel_connector)
    workspace = GoogleWorkspaceConnector(actions, cfg.google_workspace_user, cfg.google_workspace_connector)
    notion = NotionConnector(actions, cfg.notion_user, cfg.notion_connector)
    slack = SlackConnector(actions, cfg.slack_user, cfg.slack_connector)

    hire = hire_from_config(cfg)
    fingerprint = compute_hire_fingerprint(
        cfg.new_hire_first_name, cfg.new_hire_last_name, cfg.new_hire_personal_email, cfg.new_hire_start_date
    )

    if state.is_fully_provisioned(fingerprint):
        logger.info(
            f"'{hire.full_name}' ({cfg.new_hire_personal_email}, starting {cfg.new_hire_start_date}) "
            f"was already fully provisioned on a prior run -- skipping to avoid a duplicate Deel "
            f"record. Delete state/provisioned_hires.json to force a re-run."
        )
        return 0

    logger.info("Step 1: Resolving Deel legal entity and team")
    try:
        legal_entity_id = resolve_deel_legal_entity(deel, cfg.deel_legal_entity_id)
        team_id = resolve_deel_team(deel, cfg.deel_team_id)
    except ProvisioningError as e:
        logger.error(str(e))
        return 1

    seniority_name = None
    if not state.is_step_done(fingerprint, STEP_DEEL):
        seniority_name = deel.resolve_seniority_name(cfg.new_hire_seniority)
        if not seniority_name:
            real_names = ", ".join(lvl.get("name", "?") for lvl in deel.list_seniorities())
            logger.error(
                f"NEW_HIRE_SENIORITY '{cfg.new_hire_seniority}' did not match any real Deel "
                f"seniority level. Real options: {real_names}"
            )
            return 1

    if _shutdown_requested:
        raise ShutdownRequested("Shutdown requested before creating the Deel record -- no hire was created")

    # --- Step 2: Deel direct-employee creation (the one irreversible write) ---
    deel_status = "Not provisioned: Deel step not attempted"
    deel_contract_id = ""
    if state.is_step_done(fingerprint, STEP_DEEL):
        deel_status = "Already created (see prior run)"
        logger.info("Step 2: Deel record already created for this hire, skipping")
    elif cfg.new_hire_dry_run:
        logger.info(
            f"Step 2: DRY RUN -- would create Deel direct employee: {hire.full_name} "
            f"<{cfg.new_hire_personal_email}>, {cfg.new_hire_job_title} "
            f"({seniority_name}), legal_entity={legal_entity_id}, team={team_id}, "
            f"start={cfg.new_hire_start_date}, salary={cfg.new_hire_salary:g} {cfg.new_hire_currency}. "
            f"No real write was made -- set NEW_HIRE_DRY_RUN=false to actually create this hire."
        )
        return 0
    else:
        logger.info("Step 2: Creating direct employee record in Deel")
        try:
            record = deel.create_direct_employee(
                legal_entity_id=legal_entity_id,
                team_id=team_id,
                email=cfg.new_hire_personal_email,
                work_email=cfg.new_hire_work_email or cfg.new_hire_personal_email,
                country=cfg.new_hire_country,
                first_name=cfg.new_hire_first_name,
                last_name=cfg.new_hire_last_name,
                nationality=cfg.new_hire_nationality,
                job_title=cfg.new_hire_job_title,
                seniority_name=seniority_name,
                start_date=cfg.new_hire_start_date,
                salary=cfg.new_hire_salary,
                currency=cfg.new_hire_currency,
                state=cfg.new_hire_state or None,
                department_id=cfg.deel_department_id or None,
                employment_type=cfg.new_hire_employment_type,
            )
        except ConnectorError as e:
            logger.error(
                f"Step 2: Failed to create Deel direct employee: {e}\n"
                f"Deel has no delete/terminate tool for a direct employee, so if this error "
                f"happened AFTER a real record was actually created, check the Deel dashboard "
                f"directly before retrying -- do not assume this failure means nothing was created."
            )
            return 2

        deel_employee_id = record.get("id", "")
        deel_contract_id = (record.get("employment") or {}).get("contract_id", "")
        deel_status = f"Created: employee {deel_employee_id}" + (f" (contract {deel_contract_id})" if deel_contract_id else "")
        logger.info(f"[OK] Deel direct employee created: id={deel_employee_id}, contract={deel_contract_id}")
        state.mark_step_done(fingerprint, STEP_DEEL, {"employee_id": deel_employee_id, "contract_id": deel_contract_id})

    if _shutdown_requested:
        logger.warning(
            "Shutdown requested after creating the Deel record -- the hire already exists there; "
            "continuing to record it rather than leaving the Workspace/Notion/Slack steps undone silently."
        )

    provision_hire(cfg, hire, fingerprint, state, workspace, notion, slack, deel_status, deel_contract_id)

    outcomes = {
        "deel": "OK" if state.is_step_done(fingerprint, STEP_DEEL) else "FAILED",
        "workspace": "OK" if state.is_step_done(fingerprint, STEP_WORKSPACE) else "SKIPPED",
        "notion": "OK" if state.is_step_done(fingerprint, STEP_NOTION) else "FAILED",
        "slack": "OK" if state.is_step_done(fingerprint, STEP_SLACK) else "FAILED",
    }
    summary = ", ".join(f"{k.capitalize()}: {v}" for k, v in outcomes.items())
    logger.info(f"[SUMMARY] {hire.full_name}: {summary}")
    return 0


def run_scan_cycle(cfg: Config, actions, state: StateManager) -> int:
    """
    Scan mode: detect hires from deelmcp_onboarding_tracker_list (status
    INVITED) and run Workspace/Notion/Slack for whichever aren't already
    fully provisioned. Never creates a Deel record -- see module docstring.

    Returns:
      0  the scan completed (zero matching hires is also success -- a
         normal, common outcome, not an error)
      1  Deel is unreachable for the scan itself

    Never raises ConnectorError out of this function.
    """
    deel = DeelConnector(actions, cfg.deel_user, cfg.deel_connector)
    workspace = GoogleWorkspaceConnector(actions, cfg.google_workspace_user, cfg.google_workspace_connector)
    notion = NotionConnector(actions, cfg.notion_user, cfg.notion_connector)
    slack = SlackConnector(actions, cfg.slack_user, cfg.slack_connector)

    logger.info("Scanning Deel's onboarding tracker for hires at status INVITED")
    try:
        records = deel.list_onboarding_hires(statuses=["INVITED"])
    except ConnectorError as e:
        logger.error(f"Cannot scan Deel's onboarding tracker: {e}")
        return 1

    hires: List[Hire] = []
    for record in records:
        hire = hire_from_tracker_record(record)
        if hire is None:
            logger.warning(
                f"Skipping an onboarding tracker record missing a name or start date "
                f"(unique_id={record.get('unique_id', '?')}) -- cannot provision it safely"
            )
            continue
        hires.append(hire)

    if not hires:
        logger.info("No hires found at status INVITED -- nothing to provision this cycle")
        return 0

    logger.info(f"Found {len(hires)} hire(s) at status INVITED")

    for hire in hires:
        if _shutdown_requested:
            raise ShutdownRequested("Shutdown requested mid-scan -- stopping before the next hire")

        fingerprint = compute_scan_fingerprint(hire.deel_contract_id)
        if state.is_fully_provisioned(fingerprint, required_steps=(STEP_WORKSPACE, STEP_NOTION, STEP_SLACK)):
            logger.info(f"'{hire.full_name}' (contract {hire.deel_contract_id}) already fully provisioned, skipping")
            continue

        deel_status = f"Already exists in Deel (contract {hire.deel_contract_id}, detected via onboarding tracker)"
        provision_hire(cfg, hire, fingerprint, state, workspace, notion, slack, deel_status, hire.deel_contract_id)

        outcomes = {
            "workspace": "OK" if state.is_step_done(fingerprint, STEP_WORKSPACE) else "SKIPPED",
            "notion": "OK" if state.is_step_done(fingerprint, STEP_NOTION) else "FAILED",
            "slack": "OK" if state.is_step_done(fingerprint, STEP_SLACK) else "FAILED",
        }
        summary = ", ".join(f"{k.capitalize()}: {v}" for k, v in outcomes.items())
        logger.info(f"[SUMMARY] {hire.full_name}: {summary}")

    return 0


def run_cycle(cfg: Config, actions, state: StateManager) -> int:
    """Dispatch to the configured mode's cycle function."""
    if cfg.new_hire_mode == "scan":
        return run_scan_cycle(cfg, actions, state)
    return run_create_cycle(cfg, actions, state)


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
            continue
        conn = Connector(actions, connector_name, identifier)
        active = conn.check_auth()
        is_workspace = connector_name == cfg.google_workspace_connector
        if not active and not is_workspace:
            other_active = False

    if not cfg.google_workspace_user:
        logger.warning(
            f"{cfg.google_workspace_connector} (no GOOGLE_WORKSPACE_USER configured) -- "
            f"NOT CONFIGURED. Google Workspace provisioning will be skipped for this hire. "
            f"See README Prerequisites to set this up."
        )

    if not deel_active:
        # Deel is not optional in either mode: create mode needs it to
        # create the record, scan mode needs it to detect hires at all.
        logger.error(
            "Deel is not authorized. This agent cannot proceed without it -- "
            "fix authorization before re-running."
        )
        return 1

    if not other_active:
        logger.warning(
            "Notion and/or Slack are not authorized. Proceeding anyway -- the onboarding doc "
            "and/or welcome message will fail with a warning if their connector stays unauthorized."
        )

    logger.info("Step 0.5: Verifying Deel is reachable and the Notion parent page is accessible")
    deel = DeelConnector(actions, cfg.deel_user, cfg.deel_connector)
    notion = NotionConnector(actions, cfg.notion_user, cfg.notion_connector)
    try:
        verify_deel_writable(deel)
        verify_notion_parent_page(notion, cfg.notion_parent_page_id)
    except ProvisioningError as e:
        logger.error(str(e))
        return 1

    if cfg.new_hire_mode == "scan":
        logger.info(
            "NEW_HIRE_MODE=scan: hires are detected from Deel's onboarding tracker, not created by "
            "this agent. NEW_HIRE_* config (if set) is ignored."
        )
    elif cfg.new_hire_dry_run:
        logger.info(
            "NEW_HIRE_DRY_RUN=true: no real Deel record will be created this run. "
            "Set NEW_HIRE_DRY_RUN=false once you've confirmed the resolved IDs/details look correct."
        )

    if cfg.polling_mode:
        logger.info(
            f"Polling mode enabled (interval: {cfg.poll_interval_minutes}m, press Ctrl+C to stop)."
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
                if cfg.new_hire_mode == "create" and code == 0:
                    logger.info("[OK] Hire processed (or already complete) -- nothing further to do")
                    return 0
                elif code == 2:
                    logger.info("Deel creation failed -- stopping polling loop")
                    return 2
                # scan mode keeps polling on code == 0 -- new hires can
                # appear in the tracker between cycles, unlike create mode's
                # single configured hire which is a one-shot action.
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
