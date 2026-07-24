#!/usr/bin/env python3
"""
Revenue Forecast Commentary Agent: Salesforce + HubSpot -> Slack + Google Sheets

Pulls open pipeline by stage from Salesforce (SOQL) and HubSpot (deals search,
scoped to open pipeline/stage IDs), merges them into one pipeline-by-stage
view, calculates a coverage ratio against a configured quota target, flags
at-risk stages, drafts commentary (rule-based, with an optional LLM polish
pass), posts it to Slack, and logs a snapshot row per stage to Google Sheets.

Scalekit Agent Auth handles OAuth for all four connectors -- token storage,
refresh, and every API call go through actions.execute_tool(). No manual
token management, no direct API imports.

Setup:
  cp .env.example .env        # fill in your credentials
  pip install -r requirements.txt
  python run_flow.py           # run one forecast cycle and exit

Exit codes:
  0   = success (commentary posted, or already processed this period -- nothing to do)
  1   = error (config missing, provisioning failed, or 5 consecutive polling errors)
  2   = no data (no open pipeline found in either Salesforce or HubSpot this cycle)
  130 = interrupted (Ctrl+C or SIGTERM)
"""

import signal
import sys
import time
from typing import Optional

import scalekit.client
from dotenv import load_dotenv

from aggregator import (
    build_stage_segments,
    calculate_coverage_ratio,
    draft_commentary,
    flag_at_risk_stages,
)
import config as config_module
from config import Config
from connectors import (
    ConnectorError,
    GoogleSheetsConnector,
    HubSpotConnector,
    SalesforceConnector,
    SlackConnector,
)
import logging_config
from provisioning import ProvisioningError, ensure_google_sheet_tab, resolve_hubspot_open_stages
from state import StateManager
from summarizer import polish_commentary

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
    Run one full forecast cycle.

    Returns the number of stage segments found (open pipeline grouped by
    stage), or None if there was nothing to do at all (this analyst/period
    was already processed) -- distinct from 0, which means the pipeline
    pull succeeded but found zero open records in either CRM.
    """
    if state.is_processed(cfg.analyst_email, cfg.forecast_period):
        logger.info(
            f"Analyst '{cfg.analyst_email}' / period '{cfg.forecast_period}' already "
            f"processed -- skipping (delete state/processed_periods.json to reprocess)"
        )
        return None

    salesforce = SalesforceConnector(actions, cfg.salesforce_user, cfg.salesforce_connector)
    hubspot = HubSpotConnector(actions, cfg.hubspot_user, cfg.hubspot_connector)
    slack = SlackConnector(actions, cfg.slack_user, cfg.slack_connector)
    sheets = GoogleSheetsConnector(actions, cfg.google_sheets_user, cfg.google_sheets_connector)

    logger.info("Step 1: Fetching open pipeline from Salesforce and HubSpot")
    try:
        sf_records = salesforce.list_open_opportunities()
    except ConnectorError as e:
        logger.error(f"Could not fetch Salesforce opportunities: {e}")
        sf_records = []

    try:
        open_stage_labels = resolve_hubspot_open_stages(hubspot)
        hs_deals = hubspot.list_open_deals(list(open_stage_labels.keys())) if open_stage_labels else []
    except ProvisioningError as e:
        logger.error(f"Could not resolve HubSpot pipelines: {e}")
        open_stage_labels = {}
        hs_deals = []
    except ConnectorError as e:
        logger.error(f"Could not fetch HubSpot deals: {e}")
        hs_deals = []

    logger.info(
        f"Fetched {len(sf_records)} Salesforce opportunity(ies), "
        f"{len(hs_deals)} HubSpot deal(s)"
    )

    if not sf_records and not hs_deals:
        logger.warning("No open pipeline found in Salesforce or HubSpot this cycle")
        return 0

    logger.info("Step 2: Calculating coverage ratio and flagging at-risk stages")
    segments = build_stage_segments(sf_records, hs_deals, open_stage_labels)
    total_open_value = sum(segment.total_value for segment in segments.values())
    coverage_ratio = calculate_coverage_ratio(total_open_value, cfg.quota_target)
    at_risk_flags = flag_at_risk_stages(segments, total_open_value)

    logger.info(
        f"  {len(segments)} stage(s), ${total_open_value:,.0f} total open pipeline, "
        f"{coverage_ratio}x coverage (target {cfg.coverage_ratio_target}x), "
        f"{len(at_risk_flags)} at-risk stage(s)"
    )

    commentary = draft_commentary(
        segments=segments,
        at_risk_flags=at_risk_flags,
        total_open_value=total_open_value,
        coverage_ratio=coverage_ratio,
        coverage_ratio_target=cfg.coverage_ratio_target,
        quota_target=cfg.quota_target,
        forecast_period=cfg.forecast_period,
    )
    commentary = polish_commentary(
        commentary, cfg.forecast_period, cfg.openrouter_api_key, cfg.openrouter_model
    )

    logger.info("Step 3: Posting commentary to Slack")
    channel_id = slack.resolve_channel_id(cfg.slack_channel)
    if channel_id:
        try:
            slack.send_message(channel_id, commentary)
            logger.info(f"[OK] Commentary posted to Slack ({cfg.slack_channel} -> {channel_id})")
        except ConnectorError as e:
            logger.warning(f"Failed to post Slack commentary: {e}")
    else:
        logger.warning(
            f"Could not resolve Slack channel '{cfg.slack_channel}' -- skipping Slack post. "
            f"Set SLACK_CHANNEL to a channel that exists in your workspace, or a literal "
            f"channel/user ID."
        )

    logger.info("Step 4: Logging pipeline snapshot to Google Sheets")
    run_date = __import__("datetime").date.today().isoformat()
    rows_written = 0
    for label, segment in segments.items():
        source_str = "+".join(sorted(segment.sources.keys()))
        row = [
            run_date,
            cfg.analyst_email,
            cfg.forecast_period,
            label,
            source_str,
            segment.deal_count,
            round(segment.total_value, 2),
            coverage_ratio,
            "TRUE" if label in at_risk_flags else "FALSE",
        ]
        try:
            sheets.append_row(cfg.google_sheets_spreadsheet_id, cfg.google_sheets_tab_name, row)
            rows_written += 1
        except ConnectorError as e:
            logger.warning(f"Failed to log row for stage '{label}': {e}")

    logger.info(f"[OK] Logged {rows_written}/{len(segments)} stage row(s) to Google Sheets")

    state.mark_processed(cfg.analyst_email, cfg.forecast_period)
    return len(segments)


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

    logger.info("Step 0.5: Verifying Google Sheets destination and HubSpot pipelines")
    sheets = GoogleSheetsConnector(actions, cfg.google_sheets_user, cfg.google_sheets_connector)
    try:
        ensure_google_sheet_tab(sheets, cfg.google_sheets_spreadsheet_id, cfg.google_sheets_tab_name)
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
                count = run_cycle(cfg, actions, state)
                consecutive_errors = 0
                if count:
                    logger.info(f"[OK] Processed {count} stage segment(s)")
                else:
                    logger.info("Nothing new to process this cycle")
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
        count = run_cycle(cfg, actions, state)
        if count is None:
            return 0
        if count:
            logger.info(f"[OK] Processed {count} stage segment(s)")
            return 0
        logger.info("No open pipeline found in Salesforce or HubSpot this cycle")
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
