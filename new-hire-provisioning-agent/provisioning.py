"""
Startup provisioning: validate the Notion parent/template page is accessible,
and resolve Deel's legal entity + team (and optionally department) IDs that
deelmcp_org_direct_employee_create requires.

Google Workspace is deliberately NOT validated here. An unconfigured Google
Workspace connector is an expected, common state (see connectors.py
GoogleWorkspaceConnector and README Prerequisites) rather than a
misconfiguration that should block the whole run -- it's handled per-step in
run_flow.py Step 3 with graceful degradation (log a clear actionable error
for just that step, continue to Notion/Slack), not as a hard startup gate
here.
"""

import logging

from connectors import ConnectorError, ConnectorUnavailableError, DeelConnector, NotionConnector

logger = logging.getLogger(__name__)


class ProvisioningError(Exception):
    """Raised when required setup is missing and cannot be auto-created."""


def verify_notion_parent_page(notion: NotionConnector, parent_page_id: str) -> None:
    """
    Verify the configured Notion parent/hub page is reachable by the
    connected integration. Notion's API has no direct "does page X exist and
    is it shared with me" tool exposed through NotionMCP's toolset, so this
    performs a lightweight connectivity smoke test (a search call) and relies
    on the real create-page call in run_flow.py Step 4 to surface a precise
    error if parent_page_id turns out to be wrong or unshared.
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


def resolve_deel_legal_entity(deel: DeelConnector, configured_id: str) -> str:
    """
    Resolve the Deel legal entity ID that new-hire records get created
    under. If DEEL_LEGAL_ENTITY_ID is set, it's used directly. Otherwise
    deelmcp_org_legal_entity_list is queried: if exactly one legal entity
    exists in this Deel account, it's used automatically (verified live: a
    real, no-argument bulk listing tool exists for this); if there's more
    than one, this fails with a specific message rather than silently
    guessing which one new hires should be created under.
    """
    if configured_id:
        return configured_id

    try:
        entities = deel.list_legal_entities()
    except ConnectorError as e:
        raise ProvisioningError(f"Cannot reach Deel to list legal entities: {e}") from e

    if not entities:
        raise ProvisioningError(
            "No legal entities found in this Deel account. Set up at least one "
            "legal entity in the Deel dashboard before provisioning new hires."
        )
    if len(entities) > 1:
        names = ", ".join(f"{e.get('name', '?')} ({e.get('id', '?')})" for e in entities)
        raise ProvisioningError(
            f"Multiple legal entities found in this Deel account ({names}) and "
            f"DEEL_LEGAL_ENTITY_ID is not set. Set it to the exact ID of the "
            f"legal entity new hires should be created under."
        )

    entity = entities[0]
    logger.info(f"[OK] Deel legal entity resolved: {entity.get('name', '?')} ({entity['id']})")
    return entity["id"]


def resolve_deel_team(deel: DeelConnector, configured_id: str) -> str:
    """
    Resolve the Deel team ID new-hire records get assigned to, the same way
    as resolve_deel_legal_entity: use DEEL_TEAM_ID if set, otherwise
    auto-resolve if exactly one team exists, otherwise fail with a specific
    message.
    """
    if configured_id:
        return configured_id

    try:
        teams = deel.list_teams()
    except ConnectorError as e:
        raise ProvisioningError(f"Cannot reach Deel to list teams: {e}") from e

    if not teams:
        raise ProvisioningError(
            "No teams found in this Deel account. Set up at least one team in "
            "the Deel dashboard before provisioning new hires."
        )
    if len(teams) > 1:
        names = ", ".join(f"{t.get('name', '?')} ({t.get('id', '?')})" for t in teams)
        raise ProvisioningError(
            f"Multiple teams found in this Deel account ({names}) and "
            f"DEEL_TEAM_ID is not set. Set it to the exact ID of the team new "
            f"hires should be assigned to."
        )

    team = teams[0]
    logger.info(f"[OK] Deel team resolved: {team.get('name', '?')} ({team['id']})")
    return team["id"]


def verify_deel_writable(deel: DeelConnector) -> None:
    """
    Verify Deel is reachable at all by making one cheap, real read call
    (listing legal entities, which every account has at least attempted to
    set up). Raises ProvisioningError if the call fails for a reason other
    than "not configured yet", since without a working connection this agent
    cannot create new-hire records at all -- unlike the sibling
    pto-leave-request-agent's optional-connector pattern, Deel here is not
    optional: it is the only real write path this agent has.
    """
    try:
        deel.list_legal_entities()
    except ConnectorUnavailableError as e:
        raise ProvisioningError(
            f"Deel (DeelMCP) is not configured in this Scalekit workspace: {e}\n"
            f"Connect a Deel account under Agent Auth > Connections in your "
            f"Scalekit dashboard, then set DEEL_CONNECTOR to the exact "
            f"connection name shown there."
        ) from e
    except ConnectorError as e:
        raise ProvisioningError(
            f"Cannot reach Deel: {e}\n"
            f"Confirm DEEL_CONNECTOR points at an ACTIVE Deel connection with "
            f"permission to create direct employees."
        ) from e

    logger.info("[OK] Deel is reachable")
