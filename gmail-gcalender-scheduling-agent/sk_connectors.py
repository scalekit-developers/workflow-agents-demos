# sk_connectors.py — Scalekit client: tool execution + OAuth token access

import os
from typing import Any, Dict, Optional
from scalekit import ScalekitClient
from dotenv import load_dotenv

load_dotenv()

_client: Optional[ScalekitClient] = None


def _get_client() -> ScalekitClient:
    global _client
    if _client is None:
        _client = ScalekitClient(
            env_url=os.getenv("SCALEKIT_ENV_URL", ""),
            client_id=os.getenv("SCALEKIT_CLIENT_ID", ""),
            client_secret=os.getenv("SCALEKIT_CLIENT_SECRET", ""),
        )
    return _client


def execute_tool(tool_name: str, params: Dict[str, Any], identifier: str) -> Any:
    """Call a Scalekit connector tool by name and return .data."""
    client = _get_client()
    resp = client.actions.execute_tool(
        tool_name=tool_name,
        tool_input=params,
        identifier=identifier,
    )
    return resp.data if hasattr(resp, "data") else resp


def get_access_token(connection_name: str, identifier: str) -> str:
    """
    Fetch a fresh OAuth access token via Scalekit for operations that
    don't have a named connector tool (e.g. gmail draft, mark-read).
    Scalekit auto-refreshes the token on every call.
    """
    client = _get_client()
    resp = client.actions.get_connected_account(
        connection_name=connection_name,
        identifier=identifier,
    )
    account = resp.connected_account
    if account.status != "ACTIVE":
        raise RuntimeError(
            f"'{connection_name}' not ACTIVE for '{identifier}' — "
            f"authorize via Scalekit dashboard first."
        )
    tokens = account.authorization_details["oauth_token"]
    return tokens["access_token"]


def ensure_connected(connection_name: str, identifier: str) -> None:
    """Create account if missing, print auth link if not yet ACTIVE."""
    client = _get_client()
    resp = client.actions.get_or_create_connected_account(
        connection_name=connection_name,
        identifier=identifier,
    )
    account = resp.connected_account
    if account.status != "ACTIVE":
        link_resp = client.actions.get_authorization_link(
            connection_name=connection_name,
            identifier=identifier,
        )
        print(f"\n🔗 Authorize {connection_name} for {identifier}:")
        print(f"   {link_resp.link}\n")
        input("Press Enter after completing authorization...")
    print(f"✅ {connection_name} connected for {identifier}")
