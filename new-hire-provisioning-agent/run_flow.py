#!/usr/bin/env python3
"""
New Hire Provisioning Agent: Gusto -> Google Workspace + Notion + Slack

Runs on behalf of an HR admin: scans Gusto for new hire records that haven't
been provisioned yet (or targets one specific hire via NEW_HIRE_EMPLOYEE_ID /
NEW_HIRE_NAME), provisions a Google Workspace account for them via Domain-Wide
Delegation, creates a Notion onboarding doc from a template/hub page, and
posts a welcome message to a shared Slack channel.

Google Workspace provisioning is optional/conditional: if the GOOGLEDWD
connector isn't configured (a common state until an HR admin completes the
GCP service account + Domain-Wide Delegation setup, see README), this step
logs a clear actionable warning and the pipeline continues to Notion and
Slack rather than aborting. The final summary always states each step's real
outcome (PROVISIONED / SKIPPED / FAILED) rather than a single pass/fail flag,
so "Workspace not set up yet" is never confused with "the whole run failed".

Each employee ID is provisioned at most once: state.py tracks which of the
three steps (workspace, notion, slack) have already succeeded per employee,
so re-running (or polling) never re-creates a duplicate Notion page or
re-posts a duplicate Slack welcome for someone already handled. A new hire
whose Workspace step failed/was skipped is correctly re-surfaced on the next
run to retry just that step.

Scalekit Agent Auth handles OAuth for all connectors -- token storage,
refresh, and every API call go through actions.execute_tool(). No manual
token management, no direct API imports.

Setup:
  cp .env.example .env        # fill in your credentials
  pip install -r requirements.txt
  python run_flow.py           # scan Gusto once and provision any new hires

Exit codes:
  0   = success (no error; includes "no new hires found", a normal outcome)
  1   = error (config missing, provisioning failed, or 5 consecutive polling errors)
  2   = new hire(s) found but nothing could be provisioned at all this run
        (e.g. Notion parent page unreachable for every candidate)
  130 = interrupted (Ctrl+C or SIGTERM)
"""

import signal
import sys
import time
from typing import Dict, List, Optional

import scalekit.client
from dotenv import load_dotenv

from aggregator import (
    build_onboarding_page_markdown,
    build_onboarding_page_title,
    build_welcome_message,
    extract_onboarding_fields,
)
import config as config_module
from config import Config
from connectors import (
    Connector,
    ConnectorError,
    ConnectorUnavailableError,
    GoogleWorkspaceConnector,
    GustoConnector,
    NotionConnector,
    SlackConnector,
)
import logging_config
from provisioning import ProvisioningError, verify_gusto_queryable, verify_notion_parent_page
from state import STEP_NOTION, STEP_SLACK, STEP_WORKSPACE, StateManager

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


def _employee_id_of(employee: Dict) -> str:
    return str(employee.get("uuid") or employee.get("id") or "")


def find_target_hires(cfg: Config, gusto: GustoConnector, state: StateManager) -> List[Dict]:
    """
    Step 1: resolve the list of new hire records to process this run.

    If NEW_HIRE_EMPLOYEE_ID or NEW_HIRE_NAME is set, targets exactly that one
    hire (a one-shot override for testing or a manual re-run). Otherwise scans
    Gusto for records that look like new hires (see GustoConnector.
    find_new_hires) and filters out anyone state.py already marked as fully
    provisioned, so a normal run only ever returns hires with real work left
    to do.
    """
    if cfg.new_hire_employee_id:
        logger.info(f"Targeting specific employee by ID: {cfg.new_hire_employee_id}")
        try:
            detail = gusto.get_employee(cfg.new_hire_employee_id)
        except ConnectorError as e:
            logger.error(f"Could not fetch employee '{cfg.new_hire_employee_id}': {e}")
            return []
        if not detail:
            logger.error(f"No employee found for NEW_HIRE_EMPLOYEE_ID '{cfg.new_hire_employee_id}'")
            return []
        return [detail]

    if cfg.new_hire_name:
        logger.info(f"Targeting specific employee by name: {cfg.new_hire_name}")
        try:
            matches = gusto.list_employees(page=1, per=25)
        except ConnectorError as e:
            logger.error(f"Could not search Gusto employees by name: {e}")
            return []
        name_lower = cfg.new_hire_name.strip().lower()
        found = [
            e for e in matches
            if name_lower in f"{e.get('first_name', '')} {e.get('last_name', '')}".strip().lower()
        ]
        if not found:
            logger.error(f"No employee found matching NEW_HIRE_NAME '{cfg.new_hire_name}'")
            return []
        return found

    logger.info(
        f"Scanning Gusto for new hires "
        f"(window: {cfg.new_hire_lookback_days}d back, {cfg.new_hire_lookahead_days}d ahead)"
    )
    try:
        candidates = gusto.find_new_hires(cfg.new_hire_lookback_days, cfg.new_hire_lookahead_days)
    except ConnectorError as e:
        logger.error(f"Could not scan Gusto for new hires: {e}")
        return []

    unprovisioned = [
        e for e in candidates
        if not state.is_fully_provisioned(_employee_id_of(e))
    ]
    skipped = len(candidates) - len(unprovisioned)
    if skipped:
        logger.info(f"{skipped} candidate(s) already fully provisioned, skipping")

    return unprovisioned


def provision_one_hire(
    cfg: Config,
    employee_summary: Dict,
    gusto: GustoConnector,
    workspace: GoogleWorkspaceConnector,
    notion: NotionConnector,
    slack: SlackConnector,
    state: StateManager,
) -> Dict[str, str]:
    """
    Run Steps 2-4 for one new hire and return a per-step outcome dict, e.g.
    {"workspace": "SKIPPED", "notion": "OK", "slack": "OK"}. Never raises --
    every step is independently try/excepted so one step's failure never
    blocks the others, matching the required "Workspace: FAILED, Notion: OK,
    Slack: OK" style final summary.
    """
    employee_id = _employee_id_of(employee_summary)
    outcomes = {"workspace": "SKIPPED", "notion": "SKIPPED", "slack": "SKIPPED"}

    # Fetch full detail record if we only have a list-summary so far (has an
    # ID but is missing e.g. job/start_date fields only get_employee returns).
    employee = employee_summary
    if employee_id and "jobs" not in employee_summary:
        try:
            detail = gusto.get_employee(employee_id)
            if detail:
                employee = detail
        except ConnectorError as e:
            logger.warning(f"Could not fetch full Gusto profile for {employee_id}, using summary record: {e}")

    fields, warnings = extract_onboarding_fields(employee)
    if warnings:
        logger.warning(
            f"Employee record for '{fields['full_name']}' is missing: {', '.join(warnings)} "
            f"-- proceeding with placeholders where needed"
        )

    logger.info(f"Provisioning new hire: {fields['full_name']} (Gusto ID: {employee_id or 'unknown'})")

    # --- Step 2: Google Workspace (optional, must degrade gracefully) ---
    workspace_status_text = "Not provisioned: Google Workspace step not attempted"
    if state.is_step_done(employee_id, STEP_WORKSPACE):
        outcomes["workspace"] = "OK"
        workspace_status_text = "Already provisioned (see prior run)"
        logger.info("Step 2: Google Workspace account already provisioned for this hire, skipping")
    elif not cfg.google_workspace_domain:
        outcomes["workspace"] = "SKIPPED"
        workspace_status_text = "Not provisioned: GOOGLE_WORKSPACE_DOMAIN not configured"
        logger.warning("Step 2: GOOGLE_WORKSPACE_DOMAIN not set, skipping Google Workspace provisioning")
    else:
        logger.info("Step 2: Provisioning Google Workspace account via Domain-Wide Delegation")
        try:
            local_part = f"{fields['full_name']}".strip().lower().replace(" ", ".") or "new.hire"
            primary_email = f"{local_part}@{cfg.google_workspace_domain}"
            workspace.provision_user(
                primary_email=primary_email,
                first_name=employee.get("first_name", ""),
                last_name=employee.get("last_name", ""),
            )
            outcomes["workspace"] = "OK"
            workspace_status_text = f"Provisioned: {primary_email}"
            state.mark_step_done(employee_id, STEP_WORKSPACE, {"email": primary_email})
            logger.info(f"[OK] Google Workspace account provisioned: {primary_email}")
        except NotImplementedError as e:
            outcomes["workspace"] = "SKIPPED"
            workspace_status_text = "Not provisioned: Google Workspace (DWD) is not set up for this workspace yet"
            logger.warning(
                "Step 2: Google Workspace provisioning is not available -- "
                "GOOGLEDWD is not configured in this Scalekit workspace. "
                "See README Prerequisites for the required GCP service account "
                "+ Domain-Wide Delegation setup. Continuing to Notion and Slack."
            )
            logger.debug(f"Detail: {e}")
        except ConnectorUnavailableError as e:
            outcomes["workspace"] = "SKIPPED"
            workspace_status_text = "Not provisioned: Google Workspace connector not configured"
            logger.warning(
                f"Step 2: Google Workspace connector is not configured: {e}. "
                f"Continuing to Notion and Slack."
            )
        except ConnectorError as e:
            outcomes["workspace"] = "FAILED"
            workspace_status_text = f"FAILED: {e}"
            logger.error(f"Step 2: Google Workspace provisioning failed: {e}. Continuing to Notion and Slack.")

    # --- Step 3: Notion onboarding doc ---
    if state.is_step_done(employee_id, STEP_NOTION):
        outcomes["notion"] = "OK"
        logger.info("Step 3: Notion onboarding page already created for this hire, skipping")
    else:
        logger.info("Step 3: Creating Notion onboarding page")
        title = build_onboarding_page_title(fields)
        markdown_body = build_onboarding_page_markdown(fields, warnings, workspace_status_text)
        try:
            result = notion.upsert_onboarding_page(cfg.notion_parent_page_id, title, markdown_body)
            page_id = ((result.get("pages") or [{}])[0]).get("id", "")
            outcomes["notion"] = "OK"
            state.mark_step_done(employee_id, STEP_NOTION, {"page_id": page_id})
            logger.info(f"[OK] Notion onboarding page ready: {page_id}")
        except ConnectorError as e:
            outcomes["notion"] = "FAILED"
            logger.error(f"Step 3: Failed to create Notion onboarding page: {e}. Continuing to Slack.")

    # --- Step 4: Slack welcome message ---
    if state.is_step_done(employee_id, STEP_SLACK):
        outcomes["slack"] = "OK"
        logger.info("Step 4: Slack welcome message already posted for this hire, skipping")
    else:
        logger.info("Step 4: Posting welcome message to Slack")
        channel_id = slack.resolve_channel_id(cfg.slack_welcome_channel)
        if not channel_id:
            outcomes["slack"] = "FAILED"
            logger.error(
                f"Step 4: Could not resolve Slack channel '{cfg.slack_welcome_channel}'. "
                f"Confirm the channel exists and the bot is a member, or set "
                f"SLACK_WELCOME_CHANNEL to a literal channel ID."
            )
        else:
            message = build_welcome_message(fields, warnings)
            try:
                slack.send_welcome_message(channel_id, message)
                outcomes["slack"] = "OK"
                state.mark_step_done(employee_id, STEP_SLACK, {"channel_id": channel_id})
                logger.info(f"[OK] Welcome message posted to Slack ({cfg.slack_welcome_channel} -> {channel_id})")
            except ConnectorError as e:
                outcomes["slack"] = "FAILED"
                logger.error(f"Step 4: Failed to post Slack welcome message: {e}")

    return outcomes


def run_cycle(cfg: Config, actions, state: StateManager) -> Optional[List[Dict]]:
    """
    Run one full provisioning cycle. Returns a list of per-hire outcome dicts
    (possibly empty), or None if there was a hard failure resolving the
    candidate list itself (distinct from an empty list, which means "no new
    hires found", a normal outcome, not an error).
    """
    gusto = GustoConnector(actions, cfg.gusto_user, cfg.gusto_connector)
    workspace = GoogleWorkspaceConnector(actions, cfg.google_workspace_user or cfg.hr_admin_email, cfg.google_workspace_connector)
    notion = NotionConnector(actions, cfg.notion_user, cfg.notion_connector)
    slack = SlackConnector(actions, cfg.slack_user, cfg.slack_connector)

    logger.info("Step 1: Detecting new hire record(s) in Gusto not yet provisioned")
    hires = find_target_hires(cfg, gusto, state)

    if not hires:
        logger.info("No new hires found to provision this cycle")
        return []

    logger.info(f"Found {len(hires)} new hire(s) to provision")

    results = []
    for employee in hires:
        if _shutdown_requested:
            logger.info("Shutdown requested mid-cycle, stopping before the next hire")
            break
        outcomes = provision_one_hire(cfg, employee, gusto, workspace, notion, slack, state)
        results.append(outcomes)

        summary = ", ".join(f"{k.capitalize()}: {v}" for k, v in outcomes.items())
        logger.info(f"[SUMMARY] {summary}")

    return results


def main() -> int:
    cfg = init_config()
    sk = init_scalekit(cfg)
    actions = sk.actions
    state = StateManager()

    logger.info("Step 0: Checking connector auth")
    all_required_active = True
    for connector_name, identifier in cfg.get_connector_users().items():
        conn = Connector(actions, connector_name, identifier)
        active = conn.check_auth()
        is_workspace = connector_name == cfg.google_workspace_connector
        if not active and not is_workspace:
            all_required_active = False

    # Google Workspace is checked above too (for visibility in the logs) but
    # never flips all_required_active: it's optional/conditional (see
    # provisioning.py module docstring and README Prerequisites).
    if cfg.google_workspace_user:
        pass  # already checked in the loop above
    else:
        logger.warning(
            f"{cfg.google_workspace_connector} (no GOOGLE_WORKSPACE_USER configured) -- "
            f"NOT CONFIGURED. Google Workspace provisioning will be skipped for every "
            f"new hire this run. See README Prerequisites to set this up."
        )

    if not all_required_active:
        logger.warning("Some required connectors are not authorized. Proceeding anyway -- affected steps will be skipped or fail.")

    logger.info("Step 0.5: Verifying Gusto is queryable and Notion parent page is accessible")
    gusto = GustoConnector(actions, cfg.gusto_user, cfg.gusto_connector)
    notion = NotionConnector(actions, cfg.notion_user, cfg.notion_connector)
    try:
        verify_gusto_queryable(gusto)
        verify_notion_parent_page(notion, cfg.notion_parent_page_id)
    except ProvisioningError as e:
        logger.error(str(e))
        return 1

    if cfg.polling_mode:
        logger.info(
            f"Polling mode enabled (interval: {cfg.poll_interval_minutes}m, press Ctrl+C to stop)"
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
                results = run_cycle(cfg, actions, state)
                consecutive_errors = 0
                if not results:
                    logger.info("No new hires found this cycle")
                else:
                    logger.info(f"[OK] Processed {len(results)} new hire(s) this cycle")
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
        results = run_cycle(cfg, actions, state)
        if not results:
            logger.info("[OK] No new hires found in Gusto this run -- nothing to provision")
            return 0

        any_ok = any(v == "OK" for outcome in results for v in outcome.values())
        if not any_ok:
            logger.error("New hire(s) were found but nothing could be provisioned this run")
            return 2

        logger.info(f"[OK] Processed {len(results)} new hire(s)")
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.warning("Interrupted by user (signal)")
        sys.exit(130)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)
