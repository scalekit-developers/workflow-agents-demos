"""
Configuration management with validation.

All settings loaded from environment variables.
Validates on startup and provides clear error messages.
"""

import logging
import os
import sys
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


class Config:
    """Application configuration."""

    def __init__(self):
        """Load configuration from environment variables."""
        # Scalekit
        self.scalekit_env_url = os.environ.get("SCALEKIT_ENV_URL")
        self.scalekit_client_id = os.environ.get("SCALEKIT_CLIENT_ID")
        self.scalekit_client_secret = os.environ.get("SCALEKIT_CLIENT_SECRET")

        # Connector identities (the "identifier" each connected account is keyed by).
        # These can differ per connector if a shared on-call bot account authorizes
        # PagerDuty/Jira/Confluence/Slack instead of the on-call engineer's own
        # identity, but default to the engineer's email for the common case where
        # each engineer authorizes their own tools (per the brief: "delegate OAuth
        # to your engineers").
        self.pagerduty_user = os.environ.get("PAGERDUTY_USER")
        self.jira_user = os.environ.get("JIRA_USER")
        self.confluence_user = os.environ.get("CONFLUENCE_USER")
        self.slack_user = os.environ.get("SLACK_USER")

        # Connector names -- must match the exact connection name shown in the
        # Scalekit dashboard, which is often auto-suffixed per workspace
        # (e.g. "notionmcp-chAb8Lfz" in a sibling repo), not just the generic
        # provider label. Never hardcode a guessed connection name: verify yours
        # via list_connected_accounts / the dashboard before relying on defaults.
        self.pagerduty_connector = os.environ.get("PAGERDUTY_CONNECTOR", "PAGERDUTY")
        self.jira_connector = os.environ.get("JIRA_CONNECTOR", "JIRA")
        self.confluence_connector = os.environ.get("CONFLUENCE_CONNECTOR", "CONFLUENCE")
        self.slack_connector = os.environ.get("SLACK_CONNECTOR", "SLACKMCP")

        # The on-call engineer this incident is being handled on behalf of --
        # used as a label in logs and PagerDuty's required From-email header
        # for incident/note creation, not to scope any read.
        self.oncall_email = os.environ.get("ONCALL_EMAIL")

        # PagerDuty: which service this incident belongs to. Scalekit's
        # pagerduty_incident_create requires a service_id (not a name), and
        # there is no way to resolve "the right service" without a lookup, so
        # either a direct ID or a name to resolve via pagerduty_services_list
        # must be configured (see connectors.py PagerDutyConnector.resolve_service_id).
        self.pagerduty_service_id = os.environ.get("PAGERDUTY_SERVICE_ID", "")
        self.pagerduty_service_name = os.environ.get("PAGERDUTY_SERVICE_NAME", "")

        # Jira: project the incident ticket is created under, and the issue
        # type name configured in that project (varies per Jira site -- "Bug"
        # and "Incident" are both common, verify yours via jira_issue_create_
        # meta_issue_types_list before relying on the default).
        self.jira_project_key = os.environ.get("JIRA_PROJECT_KEY")
        self.jira_issue_type = os.environ.get("JIRA_ISSUE_TYPE", "Bug")

        # Optional: the real site URL (e.g. https://yourteam.atlassian.net)
        # used to build a clickable Jira ticket link in Slack/PagerDuty/
        # Confluence. jira_issue_create's response only ever carries the
        # API's cloud UUID (in "self"), never the human-readable site name --
        # constructing a browse URL from that UUID was tried and confirmed
        # live to 404, since no tool in this connector's catalog exposes the
        # real site name. Leave blank to reference tickets by key alone
        # (always correct); set this once from your own browser's address
        # bar to get real links instead.
        self.jira_site_url = os.environ.get("JIRA_SITE_URL", "").strip()

        # Confluence: the numeric space ID postmortem docs are created under.
        # Scalekit's confluence_page_create requires a numeric spaceId, and
        # the plain CONFLUENCE connector's tool catalog has no spaces-list
        # tool to resolve a space key at runtime (verified live against this
        # workspace's tool catalog -- only the separate ATLASSIANMCP provider
        # exposes atlassianmcp_getconfluencespaces), so this must be configured
        # directly, the same pattern as NOTION_BATTLECARDS_PARENT_PAGE_ID in
        # the competitive-intelligence-briefing-agent sibling repo.
        self.confluence_space_id = os.environ.get("CONFLUENCE_SPACE_ID")
        self.confluence_parent_page_id = os.environ.get("CONFLUENCE_PARENT_PAGE_ID", "")

        # Slack channel the on-call team is notified in. A channel ID
        # (C0123456789) is used as-is; a bare name is resolved via
        # slackmcp_slack_search_channels at runtime.
        self.slack_channel = os.environ.get("SLACK_CHANNEL")

        self.log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    def validate(self):
        """Validate required configuration. Fails fast if missing."""
        errors = []

        if not self.scalekit_env_url:
            errors.append("SCALEKIT_ENV_URL")
        if not self.scalekit_client_id:
            errors.append("SCALEKIT_CLIENT_ID")
        if not self.scalekit_client_secret:
            errors.append("SCALEKIT_CLIENT_SECRET")

        if not self.pagerduty_user:
            errors.append("PAGERDUTY_USER")
        if not self.jira_user:
            errors.append("JIRA_USER")
        if not self.confluence_user:
            errors.append("CONFLUENCE_USER")
        if not self.slack_user:
            errors.append("SLACK_USER")

        if not self.oncall_email:
            errors.append("ONCALL_EMAIL")
        if not self.pagerduty_service_id and not self.pagerduty_service_name:
            errors.append("PAGERDUTY_SERVICE_ID or PAGERDUTY_SERVICE_NAME")
        if not self.jira_project_key:
            errors.append("JIRA_PROJECT_KEY")
        if not self.confluence_space_id:
            errors.append("CONFLUENCE_SPACE_ID")
        if not self.slack_channel:
            errors.append("SLACK_CHANNEL")

        if errors:
            msg = f"Missing required config: {', '.join(errors)}"
            logger.error(msg)
            sys.exit(1)

    def get_connector_users(self) -> List[Tuple[str, str]]:
        """
        List of (connector name, identifier) pairs, for auth checks. A list
        rather than a dict so two connectors that happen to share the same
        connection name (e.g. a misconfigured .env) both still get checked,
        instead of one silently overwriting the other as a dict key would.
        """
        return [
            (self.pagerduty_connector, self.pagerduty_user),
            (self.jira_connector, self.jira_user),
            (self.confluence_connector, self.confluence_user),
            (self.slack_connector, self.slack_user),
        ]
