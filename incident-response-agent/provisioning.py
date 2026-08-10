"""
Startup provisioning: resolve the PagerDuty service ID and verify the Slack
channel exists before doing any real work.

PagerDuty service resolution and Slack channel resolution are done here
(Step 0.5), not deferred into the incident-creation flow itself, because a
typo'd PAGERDUTY_SERVICE_NAME or SLACK_CHANNEL is exactly the kind of static
configuration mistake this check exists to catch fast -- before an incident
has already been created in PagerDuty and Jira, at which point a failure to
notify Slack would leave the response half-complete.

Jira and Confluence are NOT resolved here beyond their auth check: unlike
PagerDuty's service and Slack's channel, JIRA_PROJECT_KEY and
CONFLUENCE_SPACE_ID are used directly as opaque IDs/keys by their create
tools with no resolution step needed, so there is nothing further to
provision -- an invalid project key or space ID surfaces as a normal,
specific ConnectorError at the point of use in run_flow.py.
"""

import logging

from connectors import ConfluenceConnector, ConnectorError, PagerDutyConnector, SlackConnector

logger = logging.getLogger(__name__)


class ProvisioningError(Exception):
    """Raised when required setup is missing and cannot be auto-resolved."""


def resolve_pagerduty_service(pagerduty: PagerDutyConnector, service_id: str, service_name: str) -> str:
    """
    Resolve the PagerDuty service ID to page against, failing fast with an
    actionable message if the configured name doesn't resolve to exactly
    one service. Paging the wrong (or no) service is worse than failing
    before any incident is created.
    """
    try:
        resolved = pagerduty.resolve_service_id(service_id, service_name)
    except ConnectorError as e:
        raise ProvisioningError(
            f"Could not resolve PagerDuty service: {e}\n"
            f"Confirm PAGERDUTY_CONNECTOR points at an ACTIVE PagerDuty connection, "
            f"and that PAGERDUTY_SERVICE_ID or PAGERDUTY_SERVICE_NAME is correct."
        ) from e

    logger.info(f"[OK] PagerDuty service resolved: {resolved}")
    return resolved


def resolve_slack_channel(slack: SlackConnector, channel_name_or_id: str) -> str:
    """
    Resolve the on-call Slack channel to a channel ID, failing fast if it
    cannot be found -- unlike a per-rep DM in a digest-style agent, silently
    skipping the on-call notification is not an acceptable degradation here.
    """
    try:
        channel_id = slack.resolve_channel_id(channel_name_or_id)
    except ConnectorError as e:
        raise ProvisioningError(
            f"Could not resolve Slack channel '{channel_name_or_id}': {e}\n"
            f"Confirm SLACK_CONNECTOR points at an ACTIVE Slack connection with "
            f"access to this channel."
        ) from e

    if not channel_id:
        raise ProvisioningError(
            f"Slack channel '{channel_name_or_id}' was not found.\n"
            f"Confirm SLACK_CHANNEL is either a valid channel ID (starts with C or G) "
            f"or an exact channel name, and that the connected Slack account can see it."
        )

    logger.info(f"[OK] Slack channel resolved: {channel_id}")
    return channel_id
