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
        env_url = os.getenv("SCALEKIT_ENV_URL", "").strip()
        client_id = os.getenv("SCALEKIT_CLIENT_ID", "").strip()
        client_secret = os.getenv("SCALEKIT_CLIENT_SECRET", "").strip()
        missing = [k for k, v in {
            "SCALEKIT_ENV_URL": env_url,
            "SCALEKIT_CLIENT_ID": client_id,
            "SCALEKIT_CLIENT_SECRET": client_secret,
        }.items() if not v]
        if missing:
            raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")
        _client = ScalekitClient(
            env_url=env_url,
            client_id=client_id,
            client_secret=client_secret,
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
    auth = account.authorization_details
    if not isinstance(auth, dict):
        raise RuntimeError(
            f"No authorization details for '{connection_name}' / '{identifier}' — "
            f"connector may use non-OAuth credentials."
        )
    if "oauth_token" not in auth:
        raise RuntimeError(
            f"No oauth_token in authorization details for '{connection_name}' / '{identifier}' — "
            f"connector may use non-OAuth credentials."
        )
    oauth_token = auth.get("oauth_token")
    if not isinstance(oauth_token, dict) or "access_token" not in oauth_token:
        raise RuntimeError(
            f"Invalid oauth_token structure for '{connection_name}' / '{identifier}'."
        )
    return oauth_token["access_token"]


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
