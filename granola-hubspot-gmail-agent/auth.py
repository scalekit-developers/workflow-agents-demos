"""Scalekit authorization handling."""
import logging
from typing import Any

logger = logging.getLogger("granola-hubspot")


def ensure_authorized(connect: Any, connector_name: str, user_id: str) -> None:
    """Check connector status. Prints a magic link if not yet authorized."""
    try:
        resp = connect.get_or_create_connected_account(
            connection_name=connector_name, identifier=user_id
        )
        if resp.connected_account.status != "ACTIVE":
            link = connect.get_authorization_link(
                connection_name=connector_name, identifier=user_id
            ).link
            logger.warning(
                f"Not authorized. {connector_name} ({user_id})\nOpen: {link}"
            )
            input("Press Enter after authorizing...")
        else:
            logger.info(f"{connector_name} ({user_id}) — ACTIVE")
    except Exception as e:
        logger.error(f"Failed to check {connector_name}: {e}")
        raise


def get_token(connect: Any, connector_name: str, user_id: str) -> str:
    """Fetch a fresh OAuth token. Always call immediately before an API call."""
    try:
        token_obj = connect.get_connected_account(
            connection_name=connector_name, identifier=user_id
        ).connected_account.authorization_details["oauth_token"]["access_token"]
        logger.debug(f"Token fetched for {connector_name}")
        return token_obj
    except (KeyError, AttributeError, TypeError) as e:
        logger.error(f"Failed to get token for {connector_name}: {e}")
        raise
