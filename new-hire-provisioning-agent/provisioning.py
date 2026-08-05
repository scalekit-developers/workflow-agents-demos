"""
Startup provisioning: validate the Notion parent/template page is accessible
to the connected integration, and that Gusto is queryable at all.

Google Workspace is deliberately NOT validated here. Unlike Notion and Gusto,
an unconfigured Google Workspace connector is an expected, common state (see
connectors.py GoogleWorkspaceConnector and README Prerequisites) rather than a
misconfiguration that should block the whole run -- it's handled per-step in
run_flow.py Step 2 with graceful degradation (log a clear actionable error for
just that step, continue to Notion/Slack), not as a hard startup gate here.
"""

import logging

from connectors import ConnectorError, ConnectorUnavailableError, GustoConnector, NotionConnector

logger = logging.getLogger(__name__)


class ProvisioningError(Exception):
    """Raised when required setup is missing and cannot be auto-created."""


def verify_notion_parent_page(notion: NotionConnector, parent_page_id: str) -> None:
    """
    Verify the configured Notion parent/hub page is reachable by the
    connected integration. Notion's API has no direct "does page X exist and
    is it shared with me" tool exposed through NotionMCP's toolset, so this
    performs a lightweight connectivity smoke test (a search call) and relies
    on the real create-page call in run_flow.py Step 3 to surface a precise
    error if parent_page_id turns out to be wrong or unshared -- mirroring
    performance-review-collector-agent's pattern of failing fast with a clear,
    actionable message rather than silently proceeding.
    """
    if not parent_page_id:
        raise ProvisioningError(
            "NOTION_PARENT_PAGE_ID (or NOTION_TEMPLATE_PAGE_ID) is not set.\n"
            "Create or choose a Notion page to act as the onboarding docs hub, "
            "share it with your Notion integration (the pages panel of your "
            "integration's settings, or the page's Connections menu in "
            "Notion), then copy its page ID from the page URL into .env."
        )

    try:
        ok = notion.verify_parent_page(parent_page_id)
    except ConnectorError as e:
        raise ProvisioningError(
            f"Cannot reach Notion via the connected integration: {e}\n"
            f"Confirm NOTION_CONNECTOR points at an ACTIVE Notion connection."
        ) from e

    if not ok:
        raise ProvisioningError(
            f"Could not verify Notion connectivity for parent page "
            f"'{parent_page_id}'. Confirm the page exists, is shared with "
            f"your Notion integration (Connections menu on the page in "
            f"Notion), and that NOTION_PARENT_PAGE_ID is the correct page ID "
            f"from its URL."
        )

    logger.info(f"[OK] Notion parent page '{parent_page_id}' is reachable")


def verify_gusto_queryable(gusto: GustoConnector) -> None:
    """
    Verify Gusto is queryable at all by making one cheap, real
    list_employees call. Raises ProvisioningError if the call fails for a
    reason other than "zero employees" (an empty company is a valid, if
    unusual, state -- see run_flow.py's "no new hires found" handling), since
    without a working list call this agent has no way to detect new hires.
    """
    try:
        gusto.list_employees(page=1, per=1)
    except ConnectorUnavailableError as e:
        raise ProvisioningError(
            f"Gusto (GustoMCP) is not configured in this Scalekit workspace: {e}\n"
            f"Connect a Gusto account under Agent Auth > Connections in your "
            f"Scalekit dashboard, then set GUSTO_CONNECTOR to the exact "
            f"connection name shown there."
        ) from e
    except ConnectorError as e:
        raise ProvisioningError(
            f"Cannot query Gusto employees: {e}\n"
            f"Confirm GUSTO_CONNECTOR points at an ACTIVE Gusto connection "
            f"with permission to read employee records. Note: Gusto OAuth "
            f"tokens are short-lived and were observed to expire mid-session "
            f"during this agent's own build/test process -- if this error "
            f"mentions 'token expired' or 'reauthentication required', "
            f"re-authorize Gusto in the Scalekit dashboard."
        ) from e

    logger.info("[OK] Gusto is queryable")
