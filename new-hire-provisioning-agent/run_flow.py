#!/usr/bin/env python3
"""
New Hire Provisioning Agent: Deel -> Google Workspace + Notion + Slack

Runs on behalf of an HR admin: creates a real direct-employee record for one
new hire in Deel (person + employment contract, under the organization's own
legal entity), provisions a Google Workspace account for them via
Domain-Wide Delegation, creates a Notion onboarding doc from a template/hub
page, and posts a welcome message to a shared Slack channel.

Deel has no "list of pending hires waiting to be onboarded" concept and no
delete/terminate tool for a direct employee anywhere in its real catalog
(both confirmed live -- see connectors.py). This means, unlike a scan-based
agent, there is nothing to detect; the new hire's details are supplied
directly (NEW_HIRE_* env vars), and a mistaken real creation cannot be
undone through this agent or any other Scalekit tool. NEW_HIRE_DRY_RUN=true
resolves every real ID (legal entity, team, seniority) and logs exactly what
WOULD be created without ever calling the real creation tool -- strongly
recommended for first-time setup verification.

Google Workspace provisioning is optional/conditional: if the GOOGLEDWD
connector isn't configured (a common state until an HR admin completes the
GCP service account + Domain-Wide Delegation setup, see README), this step
logs a clear actionable warning and the pipeline continues to Notion and
Slack rather than aborting. The final summary always states each step's real
outcome (OK / SKIPPED / FAILED) rather than a single pass/fail flag.

Re-running with the same new-hire details (first name, last name, personal
email, start date) is a safe no-op: state.py tracks which of the four steps
(deel, workspace, notion, slack) have already succeeded, so a retry never
creates a duplicate Deel record, Notion page, or Slack post.

Scalekit Agent Auth handles OAuth for all connectors -- token storage,
refresh, and every API call go through actions.execute_tool(). No manual
token management, no direct API imports.

Setup:
  cp .env.example .env        # fill in your credentials
  pip install -r requirements.txt
  python run_flow.py           # provision the configured new hire and exit

Exit codes:
  0   = success (hire fully provisioned, or already completed on a prior
        run and correctly skipped)
  1   = error (config missing, provisioning failed, Deel unreachable, or 5
        consecutive polling errors)
  2   = the Deel creation itself failed -- not a system error in the sense
        of a bug, but the one outcome serious enough not to fold into a
        generic "0 with warnings" the way Workspace/Notion/Slack failures do
  130 = interrupted (Ctrl+C or SIGTERM)
"""

import signal
import sys
import time
from typing import Optional

import scalekit.client
from dotenv import load_dotenv

from aggregator import (
    build_full_name,
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
import logging_config
from provisioning import (
    ProvisioningError,
    resolve_deel_legal_entity,
    resolve_deel_team,
    verify_deel_writable,
    verify_notion_parent_page,
)
from state import STEP_DEEL, STEP_NOTION, STEP_SLACK, STEP_WORKSPACE, StateManager, compute_hire_fingerprint

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


def run_cycle(cfg: Config, actions, state: StateManager) -> int:
    """
    Provision the configured new hire once.

    Returns:
      0  the hire was fully processed (created now, or already completed on
         a prior run and correctly skipped)
      2  the Deel creation itself failed or was rejected -- the one outcome
         serious enough not to fold into "0 with warnings" the way
         Workspace/Notion/Slack failures do
      1  a required step (config/provisioning) fails before reaching Deel

    Never raises ConnectorError out of this function; every failure mode is
    caught, logged clearly, and turned into a return code.
    """
    deel = DeelConnector(actions, cfg.deel_user, cfg.deel_connector)
    workspace = GoogleWorkspaceConnector(actions, cfg.google_workspace_user or cfg.hr_admin_email, cfg.google_workspace_connector)
    notion = NotionConnector(actions, cfg.notion_user, cfg.notion_connector)
    slack = SlackConnector(actions, cfg.slack_user, cfg.slack_connector)

    fingerprint = compute_hire_fingerprint(
        cfg.new_hire_first_name, cfg.new_hire_last_name, cfg.new_hire_personal_email, cfg.new_hire_start_date
    )
    full_name = build_full_name(cfg)

    if state.is_fully_provisioned(fingerprint):
        logger.info(
            f"'{full_name}' ({cfg.new_hire_personal_email}, starting {cfg.new_hire_start_date}) "
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
            f"Step 2: DRY RUN -- would create Deel direct employee: {full_name} "
            f"<{cfg.new_hire_personal_email}>, {cfg.new_hire_job_title} "
            f"({seniority_name}), legal_entity={legal_entity_id}, team={team_id}, "
            f"start={cfg.new_hire_start_date}, salary={cfg.new_hire_salary:g} {cfg.new_hire_currency}. "
            f"No real write was made -- set NEW_HIRE_DRY_RUN=false to actually create this hire."
        )
        deel_status = "DRY RUN -- not actually created"
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

    # --- Step 3: Google Workspace (optional, must degrade gracefully) ---
    workspace_status = "Not provisioned: Google Workspace step not attempted"
    if state.is_step_done(fingerprint, STEP_WORKSPACE):
        workspace_status = "Already provisioned (see prior run)"
        logger.info("Step 3: Google Workspace account already provisioned for this hire, skipping")
    elif not cfg.google_workspace_domain:
        workspace_status = "Not provisioned: GOOGLE_WORKSPACE_DOMAIN not configured"
        logger.warning("Step 3: GOOGLE_WORKSPACE_DOMAIN not set, skipping Google Workspace provisioning")
    else:
        logger.info("Step 3: Provisioning Google Workspace account via Domain-Wide Delegation")
        try:
            local_part = full_name.strip().lower().replace(" ", ".") or "new.hire"
            primary_email = f"{local_part}@{cfg.google_workspace_domain}"
            workspace.provision_user(
                primary_email=primary_email,
                first_name=cfg.new_hire_first_name,
                last_name=cfg.new_hire_last_name,
            )
            workspace_status = f"Provisioned: {primary_email}"
            state.mark_step_done(fingerprint, STEP_WORKSPACE, {"email": primary_email})
            logger.info(f"[OK] Google Workspace account provisioned: {primary_email}")
        except NotImplementedError as e:
            workspace_status = "Not provisioned: Google Workspace (DWD) is not set up for this workspace yet"
            logger.warning(
                "Step 3: Google Workspace provisioning is not available -- "
                "GOOGLEDWD is not configured in this Scalekit workspace. "
                "See README Prerequisites. Continuing to Notion and Slack."
            )
            logger.debug(f"Detail: {e}")
        except ConnectorUnavailableError as e:
            workspace_status = "Not provisioned: Google Workspace connector not configured"
            logger.warning(f"Step 3: Google Workspace connector is not configured: {e}. Continuing to Notion and Slack.")
        except ConnectorError as e:
            workspace_status = f"FAILED: {e}"
            logger.error(f"Step 3: Google Workspace provisioning failed: {e}. Continuing to Notion and Slack.")

    # --- Step 4: Notion onboarding doc ---
    if state.is_step_done(fingerprint, STEP_NOTION):
        logger.info("Step 4: Notion onboarding page already created for this hire, skipping")
    else:
        logger.info("Step 4: Creating Notion onboarding page")
        title = build_onboarding_page_title(cfg)
        markdown_body = build_onboarding_page_markdown(cfg, workspace_status, deel_status, deel_contract_id)
        try:
            result = notion.upsert_onboarding_page(cfg.notion_parent_page_id, title, markdown_body)
            page_id = ((result.get("pages") or [{}])[0]).get("id", "")
            state.mark_step_done(fingerprint, STEP_NOTION, {"page_id": page_id})
            logger.info(f"[OK] Notion onboarding page ready: {page_id}")
        except ConnectorError as e:
            logger.error(f"Step 4: Failed to create Notion onboarding page: {e}. Continuing to Slack.")

    # --- Step 5: Slack welcome message ---
    if state.is_step_done(fingerprint, STEP_SLACK):
        logger.info("Step 5: Slack welcome message already posted for this hire, skipping")
    else:
        logger.info("Step 5: Posting welcome message to Slack")
        channel_id = slack.resolve_channel_id(cfg.slack_welcome_channel)
        if not channel_id:
            logger.error(
                f"Step 5: Could not resolve Slack channel '{cfg.slack_welcome_channel}'. "
                f"Confirm the channel exists and the bot is a member, or set "
                f"SLACK_WELCOME_CHANNEL to a literal channel ID."
            )
        else:
            message = build_welcome_message(cfg)
            try:
                slack.send_welcome_message(channel_id, message)
                state.mark_step_done(fingerprint, STEP_SLACK, {"channel_id": channel_id})
                logger.info(f"[OK] Welcome message posted to Slack ({cfg.slack_welcome_channel} -> {channel_id})")
            except ConnectorError as e:
                logger.error(f"Step 5: Failed to post Slack welcome message: {e}")

    outcomes = {
        "deel": "OK" if state.is_step_done(fingerprint, STEP_DEEL) else "FAILED",
        "workspace": "OK" if state.is_step_done(fingerprint, STEP_WORKSPACE) else "SKIPPED",
        "notion": "OK" if state.is_step_done(fingerprint, STEP_NOTION) else "FAILED",
        "slack": "OK" if state.is_step_done(fingerprint, STEP_SLACK) else "FAILED",
    }
    summary = ", ".join(f"{k.capitalize()}: {v}" for k, v in outcomes.items())
    logger.info(f"[SUMMARY] {full_name}: {summary}")
    return 0


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
        # Unlike Notion/Slack/Workspace, Deel is the one connector this
        # agent cannot degrade around: it's the only real creation path
        # this agent has (see module docstring). Fail fast here rather
        # than let the real failure surface a few seconds later at Step 2
        # with less context.
        logger.error(
            "Deel is not authorized. This agent cannot create a real new-hire record without "
            "it -- fix authorization before re-running."
        )
        return 1

    if not other_active:
        logger.warning(
            "Notion and/or Slack are not authorized. Proceeding anyway -- the Deel creation can "
            "still succeed; the onboarding doc and/or welcome message will fail with a warning if "
            "their connector stays unauthorized."
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

    if cfg.new_hire_dry_run:
        logger.info(
            "NEW_HIRE_DRY_RUN=true: no real Deel record will be created this run. "
            "Set NEW_HIRE_DRY_RUN=false once you've confirmed the resolved IDs/details look correct."
        )

    if cfg.polling_mode:
        logger.info(
            f"Polling mode enabled (interval: {cfg.poll_interval_minutes}m, press Ctrl+C to stop). "
            f"Since provisioning one hire is a one-shot action once completed, polling here "
            f"re-checks whether the CONFIGURED hire has been fully processed yet -- it does not "
            f"re-create a hire that already completed (see state.py)."
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
                    logger.info("[OK] Hire processed (or already complete) -- nothing further to do")
                    return 0
                elif code == 2:
                    logger.info("Deel creation failed -- stopping polling loop")
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
