#!/usr/bin/env python3
"""
Performance Review Collector Agent: Airtable + Google Forms + Notion + Slack

Collects performance review feedback scoped to one manager's direct reports,
aggregates structured ratings (Airtable) and free-text feedback (Google Forms)
per employee, summarizes each employee's feedback, writes the summary to a
Notion page, and DMs the manager a Slack digest linking to each page.

Scalekit Agent Auth handles OAuth for all four connectors -- token storage,
refresh, and every API call go through actions.execute_tool(). No manual
token management, no direct API imports.

Setup:
  cp .env.example .env        # fill in your credentials
  pip install -r requirements.txt
  python run_flow.py           # run one review cycle and exit

Exit codes:
  0   = success (summaries written, or no direct reports found -- nothing to do)
  1   = error (config missing, auth failed, or 5 consecutive polling errors)
  2   = no data (no feedback found for any direct report this cycle)
  130 = interrupted (Ctrl+C or SIGTERM)
"""

import signal
import sys
import time

import scalekit.client
from dotenv import load_dotenv

from aggregator import build_employee_feedback, resolve_direct_reports
import config as config_module
from config import Config
from connectors import (
    AirtableConnector,
    ConnectorError,
    GoogleFormsConnector,
    NotionConnector,
    SlackConnector,
)
import logging_config
from provisioning import ProvisioningError, ensure_airtable_table, validate_google_form
from state import StateManager
from summarizer import summarize

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
        logger.error(f"Failed to initialize Scalekit: {e}")
        sys.exit(1)


def build_notion_summary_page(name: str, review_period: str, feedback, narrative: str) -> str:
    """Render one employee's summary page as Notion-flavored markdown."""
    lines = [f"# {name} — {review_period}", ""]

    ratings = feedback.average_ratings()
    if ratings:
        lines.append("## Average Ratings")
        lines.append("")
        lines.append("| Category | Average |")
        lines.append("|---|---|")
        for field, value in ratings.items():
            lines.append(f"| {field} | {value}/5 |")
        lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(narrative)
    lines.append("")

    comments = feedback.all_comments()
    if comments:
        lines.append("## Raw Feedback")
        lines.append("")
        for comment in comments:
            lines.append(f"- {comment}")
        lines.append("")

    lines.append(f"_Collected from {feedback.response_count()} response(s) via Airtable + Google Forms — Scalekit Agent Auth_")
    return "\n".join(lines)


def build_slack_digest(manager_email: str, review_period: str, results: list) -> str:
    """Compose the manager's Slack DM summarizing what was written for each report."""
    lines = [f"*Performance review summary ready — {review_period}*", ""]
    for entry in results:
        name = entry["name"]
        overall = entry["overall_average"]
        count = entry["response_count"]
        url = entry.get("notion_url", "")
        rating_str = f"{overall}/5 avg" if overall is not None else "no ratings"
        link = f" — <{url}|View in Notion>" if url else ""
        lines.append(f"• *{name}* — {count} response(s), {rating_str}{link}")
    lines.append("")
    lines.append("_Summaries generated and written by your Performance Review Collector Agent._")
    return "\n".join(lines)


def run_cycle(cfg: Config, actions, state: StateManager) -> int:
    """
    Run one full collection cycle for the configured manager.
    Returns the number of employees successfully summarized.
    """
    airtable = AirtableConnector(actions, cfg.airtable_user, cfg.airtable_connector)
    forms = GoogleFormsConnector(actions, cfg.google_forms_user, cfg.google_forms_connector)
    notion = NotionConnector(actions, cfg.notion_user, cfg.notion_connector)
    slack = SlackConnector(actions, cfg.slack_user, cfg.slack_connector)

    logger.info("Step 1: Fetching review responses from Airtable and Google Forms")
    try:
        airtable_records = airtable.list_all_records(
            base_id=cfg.airtable_base_id,
            table_name=cfg.airtable_table_name,
            view=cfg.airtable_view,
        )
    except ConnectorError as e:
        logger.error(f"Could not fetch Airtable records: {e}")
        airtable_records = []

    try:
        form_responses = forms.list_all_responses(cfg.google_form_id)
    except ConnectorError as e:
        logger.error(f"Could not fetch Google Forms responses: {e}")
        form_responses = []

    logger.info(
        f"Fetched {len(airtable_records)} Airtable record(s), "
        f"{len(form_responses)} Google Forms response(s)"
    )

    direct_reports = resolve_direct_reports(
        airtable_records=airtable_records,
        manager_field=cfg.airtable_manager_field,
        employee_field=cfg.airtable_employee_field,
        manager_email=cfg.manager_email,
        configured_reports=cfg.direct_reports,
    )

    if not direct_reports:
        logger.warning(f"No direct reports resolved for {cfg.manager_email} -- nothing to summarize")
        return 0

    logger.info(f"Scoped to {len(direct_reports)} direct report(s): {', '.join(direct_reports)}")

    logger.info("Step 2: Aggregating and summarizing feedback per employee")
    bundles = build_employee_feedback(
        direct_reports=direct_reports,
        airtable_records=airtable_records,
        employee_field=cfg.airtable_employee_field,
        form_responses=form_responses,
        form_employee_question_id=cfg.form_employee_question_id,
    )

    results = []
    for name, feedback in bundles.items():
        if not feedback.has_feedback():
            logger.info(f"  {name}: no feedback yet this cycle -- skipping")
            continue

        narrative = summarize(
            feedback,
            review_period=cfg.review_period,
            openrouter_api_key=cfg.openrouter_api_key,
            openrouter_model=cfg.openrouter_model,
        )
        page_markdown = build_notion_summary_page(name, cfg.review_period, feedback, narrative)

        logger.info(f"Step 3: Writing summary to Notion for {name}")
        notion_url = ""
        try:
            page_title = f"{name} — {cfg.review_period}"
            page_result = notion.upsert_employee_page(
                cfg.notion_parent_page_id, page_title, page_markdown
            )
            pages = page_result.get("pages") or [page_result]
            notion_url = (pages[0] or {}).get("url", "") if pages else ""
        except ConnectorError as e:
            logger.warning(f"Failed to write Notion page for {name}: {e}")

        results.append({
            "name": name,
            "overall_average": feedback.overall_average(),
            "response_count": feedback.response_count(),
            "notion_url": notion_url,
        })

    if not results:
        logger.info("No direct report had any feedback to summarize this cycle")
        return 0

    logger.info("Step 4: Notifying manager via Slack")
    digest = build_slack_digest(cfg.manager_email, cfg.review_period, results)
    try:
        slack.send_dm(cfg.manager_slack_id, digest)
        logger.info(f"✓ Slack digest sent to {cfg.manager_slack_id}")
    except ConnectorError as e:
        logger.warning(f"Failed to send Slack digest: {e}")

    state.mark_processed(cfg.manager_email, cfg.review_period)
    return len(results)


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

    logger.info("Step 0.5: Verifying Airtable table and Google Form are set up")
    airtable = AirtableConnector(actions, cfg.airtable_user, cfg.airtable_connector)
    forms = GoogleFormsConnector(actions, cfg.google_forms_user, cfg.google_forms_connector)
    try:
        ensure_airtable_table(
            airtable,
            base_id=cfg.airtable_base_id,
            table_name=cfg.airtable_table_name,
            employee_field=cfg.airtable_employee_field,
            manager_field=cfg.airtable_manager_field,
        )
        validate_google_form(forms, cfg.google_form_id, cfg.form_employee_question_id)
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
                return 0

            cycle += 1
            logger.info(f"Polling cycle #{cycle}")

            try:
                count = run_cycle(cfg, actions, state)
                consecutive_errors = 0
                if count:
                    logger.info(f"✓ Summarized {count} employee(s)")
                else:
                    logger.info("Nothing new to summarize this cycle")
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Error during cycle: {e}", exc_info=True)
                if consecutive_errors >= 5:
                    logger.critical("5 consecutive errors, exiting")
                    return 1

            for _ in range(cfg.poll_interval_minutes * 60):
                if _shutdown_requested:
                    logger.info("Graceful shutdown")
                    return 0
                time.sleep(1)

    else:
        count = run_cycle(cfg, actions, state)
        if count:
            logger.info(f"✓ Summarized {count} employee(s)")
            return 0
        logger.info("No feedback found for any direct report this cycle")
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
