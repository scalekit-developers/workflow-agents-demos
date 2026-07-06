"""HubSpot CRM connector — search, create, and update deals."""
import logging
from typing import Optional, Any

logger = logging.getLogger("granola-hubspot")


class HubSpotConnector:
    """HubSpot deal management."""

    def __init__(self, connect: Any, user_id: str):
        """Initialize with Scalekit connect client and user ID."""
        self.connect = connect
        self.user_id = user_id

    def search_deals(self, company: str, limit: int = 3) -> list[dict]:
        """Search for existing deals by company name."""
        try:
            result = self.connect.execute_tool(
                tool_name="hubspot_deals_search",
                identifier=self.user_id,
                tool_input={"query": company, "limit": limit},
            )
            return (result.data or {}).get("results", [])
        except Exception as e:
            logger.error(f"Failed to search deals: {e}")
            return []

    def create_deal(self, deal_name: str, stage: str, amount: Optional[int] = None) -> Optional[str]:
        """Create a new deal in HubSpot."""
        try:
            result = self.connect.execute_tool(
                tool_name="hubspot_deal_create",
                identifier=self.user_id,
                tool_input={
                    "dealname": deal_name,
                    "dealstage": stage,
                    "amount": amount or 0,
                },
            )
            deal_id = (result.data or {}).get("id")
            if deal_id:
                logger.info(f"Created deal: {deal_name} (id={deal_id})")
            return deal_id
        except Exception as e:
            logger.error(f"Failed to create deal: {e}")
            return None

    def update_deal(self, deal_id: str, properties: dict) -> bool:
        """Update an existing deal in HubSpot."""
        try:
            self.connect.execute_tool(
                tool_name="hubspot_deal_update",
                identifier=self.user_id,
                tool_input={
                    "deal_id": deal_id,
                    "properties": properties,
                },
            )
            logger.debug(f"Updated deal {deal_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to update deal {deal_id}: {e}")
            return False

    def find_or_create_deal(
        self, company: str, deal_name: str, stage: str, amount: Optional[int] = None
    ) -> tuple[str, str]:
        """Find existing deal or create new one. Returns (deal_id, deal_name)."""
        deals = self.search_deals(company)
        if deals:
            deal_id = deals[0]["id"]
            name = deals[0]["properties"].get("dealname", deal_id)
            logger.info(f"Found deal: {name} (id={deal_id})")
            return deal_id, name
        else:
            deal_id = self.create_deal(deal_name, stage, amount)
            return deal_id or "unknown", deal_name
