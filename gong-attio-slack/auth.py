"""Scalekit OAuth handling for connectors."""


def ensure_authorized(connect, connector_name: str, user_id: str) -> bool:
    """Check connector status. Return True if ACTIVE, False otherwise (no interactive prompt)."""
    resp = connect.get_or_create_connected_account(
        connection_name=connector_name, identifier=user_id
    )
    if resp.connected_account.status != "ACTIVE":
        link = connect.get_authorization_link(
            connection_name=connector_name, identifier=user_id
        ).link
        print(f"\nNot authorized for {connector_name}. Open:\n{link}\n")
        return False
    return True


def get_token(connect, connector_name: str, user_id: str) -> str:
    """Get fresh OAuth access token for connector. Requires ensure_authorized() first."""
    resp = connect.get_or_create_connected_account(
        connection_name=connector_name, identifier=user_id
    )
    if resp.connected_account.status != "ACTIVE":
        raise ValueError(f"Connector {connector_name} is not ACTIVE. Run authorization first.")
    return resp.connected_account.access_token
