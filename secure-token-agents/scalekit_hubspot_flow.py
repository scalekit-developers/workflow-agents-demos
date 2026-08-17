"""ScaleKit-style pipeline for HubSpot token demonstration.

This uses a ScaleKit broker model (client_credentials -> connector token)
instead of a direct OAuth flow. It's aligned with app.scalekit flows.

Environment variables:
- SCALEKIT_URL: Base URL for ScaleKit broker (e.g., https://auth.scalekit.com)
- SCALEKIT_CLIENT_ID: ScaleKit application client ID
- SCALEKIT_CLIENT_SECRET: ScaleKit application client secret
- SCALEKIT_CONNECTOR: Connector name (default: hubspot)
- SCALEKIT_HUBSPOT_SCOPES: Comma-separated scopes for HubSpot token (e.g., crm.objects.contacts.read)
- SAVE_TOKENS_TO_ENV=1: Persist tokens to .env (for demo only)

Run examples:
    python3 scalekit_hubspot_flow.py
"""
import os
import requests
from typing import Any, Dict, Optional
from dotenv import load_dotenv
from scalekit import ScalekitClient
import time

load_dotenv()

SCALEKIT_URL = (os.getenv("SCALEKIT_URL") or "").strip()
SCALEKIT_CLIENT_ID = (os.getenv("SCALEKIT_CLIENT_ID") or "").strip()
SCALEKIT_CLIENT_SECRET = (os.getenv("SCALEKIT_CLIENT_SECRET") or "").strip()
SCALEKIT_IDENTIFIER = (os.getenv("SCALEKIT_IDENTIFIER") or "").strip()


def sk_require_env():
    missing = []
    if not SCALEKIT_URL:
        missing.append("SCALEKIT_URL")
    if not SCALEKIT_CLIENT_ID:
        missing.append("SCALEKIT_CLIENT_ID")
    if not SCALEKIT_CLIENT_SECRET:
        missing.append("SCALEKIT_CLIENT_SECRET")
    if missing:
        raise RuntimeError(
            "Missing required ScaleKit settings: " + ", ".join(missing) +
            ". Set these in .env to use the ScaleKit flow."
        )


class ScalekitConnector:
    def __init__(self):
        self.client = ScalekitClient(
            env_url=SCALEKIT_URL,
            client_id=SCALEKIT_CLIENT_ID,
            client_secret=SCALEKIT_CLIENT_SECRET,
        )
        print(f"✅ ScaleKit connector initialized for {SCALEKIT_URL}")

    def execute_action_with_retry(
        self,
        identifier: str,
        tool: str,
        parameters: Dict[str, Any],
        max_attempts: int = 3,
        backoff: int = 2,
    ) -> Optional[Dict[str, Any]]:
        """
        Execute a Scalekit tool with exponential backoff retry logic.
        """
        for attempt in range(1, max_attempts + 1):
            try:
                print(f"🔄 Executing {tool} (attempt {attempt}/{max_attempts})")
                response = self.client.tools.execute_tool(
                    tool_name=tool,
                    identifier=identifier,
                )
                # Extract the first element of the tuple
                response = response[0] if isinstance(response, tuple) else response
                print(f"✅ Action {tool} succeeded")
                return response.data if hasattr(response, "data") else response

            except Exception as e:
                is_transient = any(
                    s in str(e).lower() for s in ["timeout", "connection", "temporary", "unavailable"]
                )
                if attempt < max_attempts and is_transient:
                    print(f"⚠️  {tool} failed (attempt {attempt}): {e}")
                    print(f"   Retrying in {backoff}s...")
                    time.sleep(backoff)
                    backoff *= 2
                else:
                    print(f"❌ {tool} failed permanently: {e}")
                    return None

        print(f"❌ {tool} failed after {max_attempts} attempts")
        return None


def fetch_hubspot_contacts(connector, identifier: str) -> Dict[str, Any]:
    """Fetch a list of contacts from HubSpot with retry logic."""
    if not identifier:
        print("❌ Missing SCALEKIT_IDENTIFIER (connected account identifier). Set it in your .env file.")
        return {"contacts": []}

    try:
        # Execute the tool with retry logic
        response = connector.execute_action_with_retry(
            identifier=identifier,
            tool="hubspot_contacts_list",
            parameters={},
        )

        if not response:
            print("❌ No response received from hubspot_contacts_list.")
            return {"contacts": []}

        # Parse and format the response data
        if hasattr(response, 'fields'):
            results_field = response.fields.get("results")
            if results_field and hasattr(results_field, 'list_value'):
                contacts_data = results_field.list_value.values
                formatted_contacts = []

                for contact in contacts_data:
                    if hasattr(contact, 'struct_value'):
                        fields = contact.struct_value.fields
                        properties = fields.get("properties").struct_value.fields if "properties" in fields else {}

                        formatted_contacts.append({
                            "id": fields.get("id").string_value if "id" in fields else None,
                            "firstname": properties.get("firstname").string_value if "firstname" in properties else None,
                            "lastname": properties.get("lastname").string_value if "lastname" in properties else None,
                            "email": properties.get("email").string_value if "email" in properties else None,
                            "url": fields.get("url").string_value if "url" in fields else None,
                        })

                if formatted_contacts:
                    print("\n[INFO] Formatted Contacts:")
                    for contact in formatted_contacts:
                        print(contact)
                else:
                    print("⚠️ No contacts found in the response.")

                return {"contacts": formatted_contacts}
        
        print("❌ Unexpected response format.")
        return {"contacts": []}

    except Exception as e:
        print(f"❌ Error fetching contacts: {e}")
        return {"contacts": []}


def main():
    print("[INFO] Simplified ScaleKit flow: Fetching HubSpot contacts.")
    connector = ScalekitConnector()
    identifier = SCALEKIT_IDENTIFIER
   
    # Fetch contacts
    print("[INFO] Fetching contacts...")
    contacts = fetch_hubspot_contacts(connector, identifier)
    print({"contacts": contacts})

    print("\n[RESULT] Simplified ScaleKit flow complete.")


if __name__ == "__main__":
    main()
