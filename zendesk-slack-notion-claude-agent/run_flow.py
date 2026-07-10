#!/usr/bin/env python3
"""
Support Triage Agent: Zendesk + Slack + Notion

Polls Zendesk for new/unprocessed tickets, classifies them by category and severity
using an LLM, searches a Notion knowledge base for matching articles, routes to the
appropriate Slack channel, and updates the Zendesk ticket with tags and internal notes.

Scalekit Agent Auth handles auth for all three connectors. Token storage, refresh,
and API calls all go through actions.execute_tool(). No manual token management.

Setup:
  cp .env.example .env
  pip install -r requirements.txt
  python run_flow.py

Exit codes:
  0   = success (tickets processed or no new tickets)
  1   = error (auth failed, config missing, or recurring errors)
  2   = no data (no new tickets to process)
  130 = interrupted (Ctrl+C or SIGTERM)
"""

import os
import sys
import time
import signal
from pathlib import Path
from dotenv import load_dotenv
import scalekit.client

from logging_config import setup_logging
from config import Config
from connectors import ZendeskConnector, SlackConnector, NotionConnector
from classifier import TicketClassifier
from state import StateManager
from triage_engine import TriageEngine

# ── Setup ──────────────────────────────────────────────────────────────────
load_dotenv()
logger = setup_logging(__name__)

# Global shutdown flag
_shutdown_requested = False


def _signal_handler(sig, frame):
    """Handle Ctrl+C and SIGTERM gracefully."""
    global _shutdown_requested
    logger.warning("Received signal, shutting down gracefully...")
    _shutdown_requested = True


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ── Configuration & Initialization ─────────────────────────────────────────
def init_config() -> Config:
    """Initialize and validate configuration."""
    config = Config()
    config.validate()
    return config


def init_scalekit(config: Config):
    """Initialize Scalekit client."""
    try:
        sk = scalekit.client.ScalekitClient(
            client_id=config.scalekit_client_id,
            client_secret=config.scalekit_client_secret,
            env_url=config.scalekit_env_url,
        )
        logger.debug("Scalekit client initialized")
        return sk
    except Exception as e:
        logger.error(f"Failed to initialize Scalekit: {e}")
        sys.exit(1)


# ── Main Pipeline ──────────────────────────────────────────────────────────
def run_once(engine: TriageEngine) -> int:
    """Run a single triage cycle."""
    count = engine.run_once()
    return count


def main() -> int:
    """
    Main entry point.
    Returns exit code: 0=success, 1=error, 2=no data, 130=interrupted.
    """
    # Initialize
    config = init_config()
    sk = init_scalekit(config)
    actions = sk.actions

    # Create connectors
    zendesk_conn = ZendeskConnector(actions, config.zendesk_user)
    slack_conn = SlackConnector(actions, config.slack_connector, config.slack_user)
    notion_conn = NotionConnector(actions, config.notion_user)

    # Create classifier and state manager
    classifier = TicketClassifier(config.openrouter_api_key)
    state_manager = StateManager()

    # Create triage engine
    engine = TriageEngine(
        zendesk_conn=zendesk_conn,
        slack_conn=slack_conn,
        notion_conn=notion_conn,
        classifier=classifier,
        state_manager=state_manager,
        config=config,
    )

    # ── Step 0: Check auth ─────────────────────────────────────────────
    logger.info("Step 0: Checking connector auth")
    all_active = True
    for conn_name in ("zendesk", config.slack_connector, "notion"):
        conn_map = {
            "zendesk": zendesk_conn,
            config.slack_connector: slack_conn,
            "notion": notion_conn,
        }
        if not conn_map[conn_name].check_auth():
            all_active = False

    if not all_active:
        logger.warning("Some connectors not authorized. Proceeding anyway.")

    # ── Polling or One-Time Mode ───────────────────────────────────────
    if config.polling_mode:
        logger.info(f"Polling mode enabled (interval: {config.poll_interval_minutes}m, press Ctrl+C to stop)")
        cycle = 0
        consecutive_errors = 0

        while True:
            if _shutdown_requested:
                logger.info("Graceful shutdown")
                return 0

            cycle += 1
            logger.info(f"Polling cycle #{cycle}")

            try:
                count = run_once(engine)
                consecutive_errors = 0

                if count > 0:
                    logger.info(f"✓ Processed {count} ticket(s)")
                elif count == 0:
                    logger.info("No new tickets, sleeping...")

            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Error during cycle: {e}", exc_info=True)
                if consecutive_errors >= 5:
                    logger.critical(f"5 consecutive errors, exiting")
                    return 1

            try:
                time.sleep(config.poll_interval_minutes * 60)
            except KeyboardInterrupt:
                logger.warning("Interrupted by user")
                return 130

        return 0
    else:
        # One-time mode
        count = run_once(engine)
        if count > 0:
            logger.info(f"✓ Processed {count} ticket(s)")
            return 0
        else:
            logger.info("No new tickets")
            return 2


if __name__ == "__main__":
    try:
        exit_code = main()
        sys.exit(exit_code)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user (signal)")
        sys.exit(130)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)
