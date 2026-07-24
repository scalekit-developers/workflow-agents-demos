#!/usr/bin/env python3
"""
Deal Room Sync Agent: Salesforce + Slack -> Google Drive

Runs on behalf of an Account Executive (AE): pulls opportunity context from
Salesforce (stage, amount, close date, next steps), captures key decisions
from relevant Slack discussion, and syncs a summary into the opportunity's
Google Drive deal room doc as a running comment log.

Scalekit Agent Auth handles OAuth for all three connectors -- token storage,
refresh, and every API call go through actions.execute_tool(). No manual
token management, no direct API imports.

Setup:
  cp .env.example .env        # fill in your credentials
  pip install -r requirements.txt
  python run_flow.py           # run one sync cycle and exit

Exit codes:
  0   = success (summary synced, or nothing new to do this cycle)
  1   = error (config missing, auth failed, provisioning failed, or 5
        consecutive polling errors)
  2   = no data (opportunity found but had no useful context to sync -- no
        Salesforce next step and no Slack discussion found)
  130 = interrupted (Ctrl+C or SIGTERM)
"""

import signal
import sys
import time
from typing import Optional

import scalekit.client
from dotenv import load_dotenv

from aggregator import DealContext, build_deal_summary
import config as config_module
from config import Config
from connectors import (
    ConnectorError,
    GoogleDriveConnector,
    SalesforceConnector,
    SlackConnector,
    split_slack_text_blob,
)
import logging_config
from provisioning import ProvisioningError, ensure_deal_room_doc, ensure_opportunity_findable
from state import StateManager

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


def run_cycle(cfg: Config, actions, state: StateManager) -> Optional[int]:
    """
    Run one full sync cycle for the configured opportunity.

    Returns 1 if the summary was synced, 0 if the opportunity had nothing
    useful to sync this cycle, or None if this (opportunity, cycle) pair was
    already synced -- distinct from 0, which means real work was attempted
    but found no meaningful context.
    """
    sync_cycle = time.strftime("%Y-%m-%d")  # one sync per opportunity per day by default

    salesforce = SalesforceConnector(actions, cfg.salesforce_user, cfg.salesforce_connector)
    slack = SlackConnector(actions, cfg.slack_user, cfg.slack_connector)
    drive = GoogleDriveConnector(actions, cfg.google_drive_user, cfg.google_drive_connector)

    logger.info("Step 1: Fetching opportunity context from Salesforce")
    try:
        opportunity = ensure_opportunity_findable(salesforce, cfg.opportunity_id, cfg.opportunity_name)
    except ProvisioningError as e:
        logger.error(str(e))
        raise

    deal = DealContext(opportunity)

    if state.is_processed(deal.opportunity_id, sync_cycle):
        logger.info(
            f"Opportunity '{deal.name}' already synced for cycle '{sync_cycle}' -- "
            f"skipping (delete state/synced_cycles.json to force a re-sync)"
        )
        return None

    logger.info(
        f"  {deal.name} | Stage: {deal.stage} | Amount: {deal.amount} | "
        f"Close: {deal.close_date or 'not set'}"
    )

    logger.info("Step 2: Capturing key decisions from relevant Slack discussion")
    keyword = cfg.effective_search_keyword(deal.name)
    try:
        if cfg.slack_channel_id:
            raw_text = slack.read_channel(cfg.slack_channel_id, limit=cfg.slack_message_limit)
        else:
            raw_text = slack.search_relevant_messages(keyword, limit=cfg.slack_message_limit)
        excerpts = split_slack_text_blob(raw_text)
        deal.add_slack_excerpts(excerpts)
        logger.info(f"  Found {len(deal.slack_excerpts)} relevant Slack excerpt(s) for '{keyword}'")
    except ConnectorError as e:
        logger.warning(f"Could not fetch Slack context: {e}")

    if not deal.next_step and not deal.has_slack_context():
        logger.info("No Salesforce next step and no Slack discussion found -- nothing meaningful to sync")
        return 0

    logger.info("Step 3: Syncing summary to the Google Drive deal room doc")
    summary = build_deal_summary(deal, ae_email=cfg.ae_email, sync_label=sync_cycle)

    try:
        doc = ensure_deal_room_doc(
            drive,
            doc_id=cfg.deal_room_doc_id,
            doc_name=cfg.deal_room_doc_name,
            folder_id=cfg.deal_room_folder_id,
            opportunity_name=deal.name,
        )
    except ProvisioningError as e:
        logger.error(str(e))
        raise

    try:
        drive.sync_deal_summary(doc.get("id"), summary)
        logger.info(f"✓ Summary synced to deal room doc '{doc.get('name')}'")
    except ConnectorError as e:
        logger.error(f"Failed to sync summary to Drive: {e}")
        raise

    state.mark_processed(deal.opportunity_id, sync_cycle)
    return 1


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
        logger.warning(
            "Some connectors are not authorized. Proceeding anyway -- affected "
            "steps will be skipped or fail with a clear error."
        )

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
                result = run_cycle(cfg, actions, state)
                consecutive_errors = 0
                if result == 1:
                    logger.info("✓ Deal room synced")
                elif result == 0:
                    logger.info("Nothing meaningful to sync this cycle")
                else:
                    logger.info("Already synced for this cycle")
            except (ProvisioningError, ConnectorError) as e:
                consecutive_errors += 1
                logger.error(f"Error during cycle: {e}")
                if consecutive_errors >= 5:
                    logger.critical("5 consecutive errors, exiting")
                    return 1
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Unexpected error during cycle: {e}", exc_info=True)
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
            result = run_cycle(cfg, actions, state)
        except (ProvisioningError, ConnectorError):
            return 1

        if result is None:
            return 0
        if result == 1:
            logger.info("✓ Deal room synced")
            return 0
        logger.info("Opportunity had nothing meaningful to sync this cycle")
        return 2


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
