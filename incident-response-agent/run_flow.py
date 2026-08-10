#!/usr/bin/env python3
"""
Incident Response Agent: PagerDuty -> Jira -> Slack -> Confluence

Runs on behalf of an on-call engineer: given an incident description (title,
severity, affected service), triggers a PagerDuty page to on-call, opens a
Jira incident ticket with severity and context, posts to the on-call Slack
channel with the Jira link, and creates a Confluence postmortem doc from a
template -- one incident per invocation, in that order, per the brief:

  1. Authorize PagerDuty + Jira + Confluence + Slack
  2. Detect alert and trigger PagerDuty page to on-call
  3. Open Jira incident ticket with severity and context
  4. Post to on-call Slack channel with Jira link
  5. Create Confluence postmortem doc from template

Scalekit Agent Auth handles OAuth for all four connectors -- token storage,
refresh, and every API call go through actions.execute_tool(). No manual
token management, no direct API imports.

This agent does not itself poll or subscribe to any alerting system --
"alert detection" is the caller's responsibility (an alertmanager/Datadog
webhook handler, a Slack slash command, an on-call engineer running this by
hand). The incident is described via CLI arguments; this keeps the agent
honest about what it can verify (four real, Scalekit-connected tools) versus
what would require guessing at a fifth, unspecified monitoring integration.

Setup:
  cp .env.example .env        # fill in your credentials
  pip install -r requirements.txt
  python run_flow.py --title "API latency spike in prod" --severity high \\
      --service-name "Production API"

Exit codes:
  0   = success (incident created across all four services, or already
        handled in a prior run for the same --incident-key)
  1   = error (config missing, provisioning failed, a required connector
        unreachable, or a create step failed)
  130 = interrupted (Ctrl+C or SIGTERM)
"""

import argparse
import datetime
import html
import signal
import sys
from typing import Optional

import scalekit.client
from dotenv import load_dotenv

from config import Config
from connectors import (
    ConfluenceConnector,
    Connector,
    ConnectorError,
    ConnectorUnavailableError,
    JiraConnector,
    PagerDutyConnector,
    SlackConnector,
    page_url,
)
import logging_config
from provisioning import ProvisioningError, resolve_pagerduty_service, resolve_slack_channel
from state import StateManager, compute_incident_key

load_dotenv()
logger = logging_config.setup_logging(__name__)

_shutdown_requested = False


class ShutdownRequested(Exception):
    """Raised to unwind cleanly to exit code 130 when a shutdown signal arrives before any irreversible action (paging on-call) has been taken."""


def _signal_handler(sig, frame):
    global _shutdown_requested
    logger.warning("Received signal, shutting down gracefully...")
    _shutdown_requested = True


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


SEVERITY_TO_URGENCY = {"critical": "high", "high": "high", "medium": "low", "low": "low"}
SEVERITY_TO_JIRA_PRIORITY = {"critical": "Highest", "high": "High", "medium": "Medium", "low": "Low"}


def parse_args(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Page on-call and open an incident for a detected alert."
    )
    parser.add_argument("--title", required=True, help="Short incident title, e.g. 'API latency spike in prod'")
    parser.add_argument(
        "--severity", default="high", choices=["critical", "high", "medium", "low"],
        help="Incident severity (default: high)"
    )
    parser.add_argument("--description", default="", help="Additional context / alert details")
    parser.add_argument(
        "--incident-key", default="",
        help="Deduplication key for this alert (e.g. from the monitoring system). "
             "Defaults to a key derived from --title if not given -- provide a real "
             "one whenever the upstream alert source has one, since two different "
             "alerts that happen to share a title would otherwise be deduplicated "
             "against each other."
    )
    return parser.parse_args(argv)


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


def run_incident_response(
    cfg: Config, actions, state: StateManager, title: str, severity: str, description: str, incident_key: str
) -> dict:
    """
    Run the five-step incident response flow for one alert. Returns a dict
    describing what was created (or, if this incident_key was already
    handled in a prior run, what was created THEN -- this function is
    idempotent per incident_key and never re-runs a completed flow).

    Raises ProvisioningError / ConnectorError for failures that should stop
    the whole run with a specific, actionable message (see main()). A
    partially-completed run (e.g. PagerDuty paged but Jira failed) is NOT
    silently retried from scratch on the next invocation with the same
    incident_key -- see the mid-flow failure handling below, which records
    partial progress so a retry does not re-page on-call.
    """
    key = compute_incident_key(incident_key)
    prior = state.get_handled(key)
    if prior:
        logger.info(f"Incident key '{incident_key}' already handled in a prior run, returning that outcome")
        return prior

    pagerduty = PagerDutyConnector(actions, cfg.pagerduty_user, cfg.pagerduty_connector)
    jira = JiraConnector(actions, cfg.jira_user, cfg.jira_connector)
    confluence = ConfluenceConnector(actions, cfg.confluence_user, cfg.confluence_connector)
    slack = SlackConnector(actions, cfg.slack_user, cfg.slack_connector)

    logger.info("Step 0.5: Resolving PagerDuty service and Slack channel")
    service_id = resolve_pagerduty_service(pagerduty, cfg.pagerduty_service_id, cfg.pagerduty_service_name)
    channel_id = resolve_slack_channel(slack, cfg.slack_channel)

    if _shutdown_requested:
        raise ShutdownRequested("Shutdown requested before paging on-call -- no incident was created")

    logger.info(f"Step 1: Triggering PagerDuty page for service {service_id}")
    urgency = SEVERITY_TO_URGENCY.get(severity, "high")
    incident = pagerduty.create_incident(
        title=title,
        service_id=service_id,
        from_email=cfg.oncall_email,
        incident_key=incident_key,
        urgency=urgency,
        body_details=description or None,
    )
    pd_incident_id = incident.get("id")
    pd_incident_number = incident.get("incident_number")
    pd_incident_url = incident.get("html_url") or ""
    logger.info(f"[OK] PagerDuty incident #{pd_incident_number} triggered ({pd_incident_id})")

    if _shutdown_requested:
        logger.warning("Shutdown requested after paging on-call -- PagerDuty incident already exists, continuing to record its ticket")

    logger.info("Step 2: Opening Jira incident ticket")
    jira_summary = f"[{severity.upper()}] {title}"
    jira_description = (
        f"{description}\n\n"
        f"Severity: {severity}\n"
        f"PagerDuty incident: {pd_incident_url or pd_incident_id}\n"
        f"Triggered by: {cfg.oncall_email}"
    ).strip()
    try:
        jira_issue = jira.create_issue(
            project_key=cfg.jira_project_key,
            summary=jira_summary,
            issue_type=cfg.jira_issue_type,
            description=jira_description,
            priority_name=SEVERITY_TO_JIRA_PRIORITY.get(severity),
            labels=["incident", severity],
        )
    except ConnectorError as e:
        # PagerDuty has already paged on-call -- record that much so a retry
        # with the same incident_key does not page a second time, then
        # re-raise so main() reports this specific, actionable failure.
        state.mark_handled(key, {
            "status": "partial", "step_failed": "jira_issue_create",
            "pagerduty_incident_id": pd_incident_id, "pagerduty_incident_number": pd_incident_number,
        })
        raise ConnectorError(
            f"PagerDuty incident #{pd_incident_number} was triggered, but creating the Jira "
            f"ticket failed: {e}\nCheck JIRA_PROJECT_KEY and JIRA_ISSUE_TYPE are valid for "
            f"this Jira site (run jira_issue_create_meta_issue_types_list to see valid "
            f"issue types for your project), then re-run with the same --incident-key -- "
            f"PagerDuty will not be re-paged."
        ) from e

    jira_key = jira_issue.get("key") or jira_issue.get("id")
    # Jira's create response only carries the API's cloud UUID (in "self",
    # e.g. https://api.atlassian.com/ex/jira/{cloud_id}/...), never the
    # site's real human-readable subdomain -- constructing a browse URL from
    # the cloud UUID was tried and confirmed live to 404, since Jira Cloud's
    # browse URL needs the actual site name, which no tool in this
    # connector's catalog exposes. JIRA_SITE_URL, if the operator sets it
    # once from their own browser's address bar, is used to build a real
    # link; otherwise the ticket is referenced by its key alone, which is
    # always correct and never a guess.
    jira_url = f"{cfg.jira_site_url.rstrip('/')}/browse/{jira_key}" if cfg.jira_site_url and jira_key else ""
    logger.info(f"[OK] Jira ticket {jira_key} created" + (f" ({jira_url})" if jira_url else ""))

    try:
        pagerduty.add_note(pd_incident_id, f"Jira ticket: {jira_url or jira_key}", cfg.oncall_email)
    except ConnectorError as e:
        logger.warning(f"Could not link Jira ticket back onto the PagerDuty incident (non-fatal): {e}")

    logger.info(f"Step 3: Notifying on-call Slack channel")
    # Slack markdown link syntax <url|label> (verified live in the sibling
    # repos) renders a clickable label instead of a raw pasted URL, for
    # every link that actually has one -- PagerDuty's html_url always does;
    # Jira's only does when JIRA_SITE_URL is set (see identify_rep's
    # equivalent honesty tradeoff in the sibling repo's docstring: a bare
    # key/number is shown instead of guessing a broken link).
    pagerduty_line = f"<{pd_incident_url}|#{pd_incident_number}>" if pd_incident_url else f"#{pd_incident_number}"
    jira_line = f"<{jira_url}|{jira_key}>" if jira_url else jira_key
    slack_text = (
        f":rotating_light: *{severity.upper()} incident triggered*\n"
        f"*{title}*\n"
        f"*PagerDuty:* {pagerduty_line}\n"
        f"*Jira:* {jira_line}"
    )
    if description:
        slack_text += f"\n*Details:* {description}"
    try:
        slack.send_message(channel_id, slack_text)
        logger.info("[OK] Slack on-call channel notified")
    except ConnectorError as e:
        logger.warning(f"Slack notification failed (non-fatal, incident and ticket already exist): {e}")

    logger.info("Step 4: Creating Confluence postmortem doc")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    # Confluence's "storage" format is real XHTML -- title/description are
    # caller-supplied free text (CLI args or an upstream alert payload), so
    # they are HTML-escaped before being embedded, and each link is a real
    # <a href> anchor rather than a raw pasted URL, matching what the Slack
    # message does with Slack's own <url|label> syntax.
    postmortem_title = f"Postmortem: {title} ({now})"
    pagerduty_html = (
        f'<a href="{html.escape(pd_incident_url)}">#{pd_incident_number}</a>'
        if pd_incident_url else f"#{pd_incident_number}"
    )
    jira_html = f'<a href="{html.escape(jira_url)}">{html.escape(jira_key)}</a>' if jira_url else html.escape(jira_key)
    postmortem_body = (
        f"<h2>Summary</h2><p>{html.escape(description) or 'TBD'}</p>"
        f"<h2>Incident details</h2>"
        f"<ul>"
        f"<li>Severity: {html.escape(severity)}</li>"
        f"<li>PagerDuty incident: {pagerduty_html}</li>"
        f"<li>Jira ticket: {jira_html}</li>"
        f"<li>Triggered by: {html.escape(cfg.oncall_email)}</li>"
        f"<li>Date: {now}</li>"
        f"</ul>"
        f"<h2>Timeline</h2><p>TBD</p>"
        f"<h2>Root cause</h2><p>TBD</p>"
        f"<h2>Action items</h2><p>TBD</p>"
    )
    confluence_page = None
    confluence_page_url = ""
    try:
        confluence_page = confluence.create_page(
            space_id=cfg.confluence_space_id,
            title=postmortem_title,
            body_value=postmortem_body,
            parent_id=cfg.confluence_parent_page_id or None,
        )
        # Unlike Jira, Confluence's create response DOES carry a usable
        # human-facing link (see page_url()'s docstring for why) -- no
        # separate site-URL config needed, verified live.
        confluence_page_url = page_url(confluence_page)
        logger.info(
            f"[OK] Confluence postmortem doc created: {postmortem_title}"
            + (f" ({confluence_page_url})" if confluence_page_url else "")
        )
        # A short follow-up message, not folded into Step 3's notification,
        # since Confluence creation happens after Slack is notified -- the
        # on-call channel would otherwise never see the postmortem link at
        # all, only the console log. Failing this is non-fatal, same as the
        # other post-creation steps: the doc already exists regardless.
        if confluence_page_url:
            try:
                slack.send_message(channel_id, f":page_facing_up: Postmortem doc: <{confluence_page_url}|{postmortem_title}>")
            except ConnectorError as e:
                logger.warning(f"Could not post the postmortem link to Slack (non-fatal, doc already exists): {e}")
    except ConnectorError as e:
        logger.warning(f"Confluence postmortem creation failed (non-fatal, incident/ticket/notification already exist): {e}")

    outcome = {
        "status": "complete",
        "pagerduty_incident_id": pd_incident_id,
        "pagerduty_incident_number": pd_incident_number,
        "pagerduty_incident_url": pd_incident_url,
        "jira_key": jira_key,
        "jira_url": jira_url,
        "confluence_page_id": (confluence_page or {}).get("id") if confluence_page else None,
        "confluence_page_url": confluence_page_url,
    }
    state.mark_handled(key, outcome)
    return outcome


def main() -> int:
    args = parse_args(sys.argv[1:])
    cfg = init_config()
    sk = init_scalekit(cfg)
    actions = sk.actions
    state = StateManager()

    logger.info("Step 0: Checking connector auth")
    all_active = True
    for connector_name, identifier in cfg.get_connector_users():
        conn = Connector(actions, connector_name, identifier)
        if not conn.check_auth():
            all_active = False

    if not all_active:
        logger.error(
            "One or more connectors are not authorized. Unlike a digest agent where a "
            "single unavailable connector can degrade gracefully, every step of incident "
            "response (page, ticket, notify, document) depends on its connector being "
            "ACTIVE -- fix authorization before re-running."
        )
        return 1

    if _shutdown_requested:
        logger.info("Graceful shutdown requested before any incident was created")
        return 130

    incident_key = args.incident_key or args.title

    try:
        outcome = run_incident_response(
            cfg, actions, state, args.title, args.severity, args.description, incident_key
        )
    except ShutdownRequested as e:
        logger.info(str(e))
        return 130
    except ProvisioningError as e:
        logger.error(str(e))
        return 1
    except ConnectorUnavailableError as e:
        logger.error(
            f"A required connector is not configured in this Scalekit workspace: {e}\n"
            f"Add the missing connection under Agent Auth > Connections in the Scalekit "
            f"dashboard, complete its OAuth flow, then set the matching *_CONNECTOR env "
            f"var to the exact connection name shown there. See README Prerequisites."
        )
        return 1
    except ConnectorError as e:
        logger.error(str(e))
        return 1

    if outcome.get("status") == "partial":
        logger.error(
            f"Incident response is incomplete for this run (see the error above); "
            f"PagerDuty incident #{outcome.get('pagerduty_incident_number')} was triggered "
            f"but later steps did not finish."
        )
        return 1

    logger.info(
        f"[SUMMARY] PagerDuty #{outcome.get('pagerduty_incident_number')} triggered, "
        f"Jira {outcome.get('jira_key')} opened, Slack notified, "
        f"Confluence {'created' if outcome.get('confluence_page_id') else 'skipped (see warnings above)'}"
    )
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
