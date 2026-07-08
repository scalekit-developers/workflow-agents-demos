"""Scalekit OAuth handling for connectors."""


def ensure_authorized(connect, connector_name: str, user_id: str) -> None:
    """Check connector status. Print magic link if not authorized."""
    resp = connect.get_or_create_connected_account(
        connection_name=connector_name, identifier=user_id
    )
    if resp.connected_account.status != "ACTIVE":
        link = connect.get_authorization_link(
            connection_name=connector_name, identifier=user_id
        ).link
        print(f"\nNot authorized for {connector_name}. Open:\n{link}\n")
        input("Press Enter after authorizing...")


def get_token(connect, connector_name: str, user_id: str) -> str:
    """Get fresh OAuth access token for connector."""
    resp = connect.get_or_create_connected_account(
        connection_name=connector_name, identifier=user_id
    )
    return resp.connected_account.access_token
