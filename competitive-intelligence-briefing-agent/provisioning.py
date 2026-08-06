"""
Startup provisioning: verify the Notion battlecards parent page is reachable
before doing any real work.

Gong is deliberately NOT part of this startup check. Whether GONG is
connected in this Scalekit workspace is a per-run data-availability
condition, not a static configuration mistake this agent can validate once
at startup and then rely on: Scalekit connections can be authorized,
expire, or be revoked between runs (verified live in a sibling repo's build,
where GUSTOMCP flipped from ACTIVE to EXPIRED mid-session), and a startup
check would either have to (a) hard-block every run whenever Gong happens to
be temporarily unavailable, even though Notion/Slack are perfectly usable
and the operator may want visibility into that, or (b) duplicate the same
"is Gong reachable" logic that Step 1 already needs to run anyway to fetch
real data. Step 0 (auth check) reports Gong's status for visibility without
blocking; Step 1 in run_flow.py is where a genuinely unreachable Gong
produces the specific, actionable failure -- see run_flow.py Step 1 and
ConnectorUnavailableError in connectors.py.

Notion IS checked here because NOTION_BATTLECARDS_PARENT_PAGE_ID is exactly
the kind of static configuration mistake this check exists to catch fast:
a typo'd or since-deleted page ID would otherwise only surface deep into
Step 2, after Gong data has already been fetched and processed.
"""

import logging

from connectors import NotionConnector

logger = logging.getLogger(__name__)


class ProvisioningError(Exception):
    """Raised when required setup is missing and cannot be auto-created."""


def verify_notion_battlecards_parent(notion: NotionConnector, parent_page_id: str) -> None:
    """
    Confirm the configured Notion battlecards parent page exists and is
    reachable by the connected integration. Raises ProvisioningError with an
    actionable message if not -- this agent never creates the parent page
    itself, since a PMM is expected to have already set up their battlecards
    library in Notion (see README Prerequisites).
    """
    # verify_parent_page() catches ConnectorError internally and returns False
    # on any failure (see its docstring), so there is no separate "reachable
    # but errored" exception path to handle here -- only the boolean result.
    reachable = notion.verify_parent_page(parent_page_id)

    if not reachable:
        raise ProvisioningError(
            f"Notion battlecards parent page '{parent_page_id}' was not found or is "
            f"empty/inaccessible.\n"
            f"Create a parent page in Notion (e.g. titled 'Competitive Battlecards'), "
            f"share it with your connected Notion integration, and set "
            f"NOTION_BATTLECARDS_PARENT_PAGE_ID to its page ID from the URL."
        )

    logger.info(f"[OK] Notion battlecards parent page '{parent_page_id}' is reachable")
