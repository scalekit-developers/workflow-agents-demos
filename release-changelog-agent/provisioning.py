"""
Startup provisioning: work out which release range to write up, and confirm
the publish destinations exist before any changelog is generated.

The release range is the one piece of config most likely to be wrong or
omitted, so it is resolved with a documented fallback chain rather than
demanding the operator supply both tags on every run.
"""

import logging
from typing import List, Optional, Tuple

from connectors import ConfluenceConnector, ConnectorError, GitHubConnector

logger = logging.getLogger(__name__)


class ProvisioningError(Exception):
    """Raised when required setup is missing and cannot be auto-resolved."""


def resolve_release_range(
    github: GitHubConnector,
    owner: str,
    repo: str,
    previous_tag: str = "",
    current_tag: str = "",
) -> Tuple[str, str]:
    """
    Decide which two refs bound this release.

    Resolution order, most explicit first:
      1. Both tags configured -- used as-is.
      2. Only CURRENT_TAG set -- the previous tag is the next one down the
         repo's tag list.
      3. Neither set -- the two most recent tags, i.e. "what shipped in the
         latest tag".
      4. No tags at all -- fall back to the latest published Release, and
         failing that raise, because a changelog with no baseline would
         silently cover the entire history of the repo.

    Returns (previous_tag, current_tag).
    """
    if previous_tag and current_tag:
        logger.info(f"[OK] Using configured range {previous_tag}...{current_tag}")
        return previous_tag, current_tag

    try:
        tags = github.list_tags(owner, repo, per_page=100)
    except ConnectorError as e:
        raise ProvisioningError(
            f"Cannot list tags for {owner}/{repo}: {e}\n"
            f"Confirm GITHUB_CONNECTOR points at an ACTIVE GitHub connection with "
            f"read access to this repository."
        ) from e

    names = [str(t.get("name")) for t in tags if t.get("name")]

    if not names:
        # Some repos publish Releases without lightweight tags being listable.
        latest = github.latest_release_tag(owner, repo)
        if latest:
            raise ProvisioningError(
                f"{owner}/{repo} has a published release ({latest}) but no listable tags, "
                f"so the previous tag cannot be inferred. Set PREVIOUS_TAG and CURRENT_TAG "
                f"explicitly."
            )
        raise ProvisioningError(
            f"{owner}/{repo} has no tags, so there is no release baseline to diff against. "
            f"Tag a release first, or set PREVIOUS_TAG and CURRENT_TAG explicitly."
        )

    if current_tag:
        if current_tag not in names:
            raise ProvisioningError(
                f"CURRENT_TAG '{current_tag}' not found in {owner}/{repo}. "
                f"Recent tags: {', '.join(names[:10])}"
            )
        index = names.index(current_tag)
        if index + 1 >= len(names):
            raise ProvisioningError(
                f"'{current_tag}' is the oldest tag in {owner}/{repo}, so there is no "
                f"previous tag to diff against. Set PREVIOUS_TAG explicitly."
            )
        resolved = (names[index + 1], current_tag)
    else:
        if len(names) < 2:
            raise ProvisioningError(
                f"{owner}/{repo} has only one tag ('{names[0]}'), so there is no previous "
                f"release to diff against. Set PREVIOUS_TAG and CURRENT_TAG explicitly."
            )
        resolved = (names[1], names[0])

    logger.info(f"[OK] Resolved release range {resolved[0]}...{resolved[1]} from repo tags")
    return resolved


def resolve_confluence_space(confluence: ConfluenceConnector, space_key: str) -> str:
    """
    Map a Confluence space key to the numeric space id the create-page API
    requires. Raises ProvisioningError listing the visible keys when it misses.
    """
    try:
        space_id = confluence.resolve_space_id(space_key)
    except ConnectorError as e:
        raise ProvisioningError(
            f"Cannot list Confluence spaces: {e}\n"
            f"Confirm CONFLUENCE_CONNECTOR points at an ACTIVE Confluence connection."
        ) from e

    if space_id:
        logger.info(f"[OK] Confluence space '{space_key}' -> id {space_id}")
        return space_id

    # Re-list only to build a useful error. Naming the keys the account CAN
    # see is the difference between "not found" and an actionable message.
    try:
        visible = ", ".join(
            str(s.get("key")) for s in confluence.list_spaces() if s.get("key")
        ) or "(none visible)"
    except ConnectorError:
        visible = "(could not list spaces)"

    raise ProvisioningError(
        f"Confluence space '{space_key}' not found. Spaces visible to this account: "
        f"{visible}. Set CONFLUENCE_SPACE_KEY to one of them."
    )
