"""Attio connector for deal and company data."""


class AttioConnector:
    """Fetch deals and companies from Attio."""

    def __init__(self, connect, user_id: str):
        self.connect = connect
        self.user_id = user_id

    def search_deals(self, company_name: str, limit: int = 5) -> list:
        """Search for deals by company name."""
        result = self.connect.execute_tool(
            tool_name="attio_search_records",
            identifier=self.user_id,
            tool_input={"object_type": "deals", "query": company_name, "limit": limit},
            connection_name="attio",
        )
        return result.data.get("results", []) if result.data else []

    def get_deal(self, deal_id: str) -> dict:
        """Get detailed deal information."""
        result = self.connect.execute_tool(
            tool_name="attio_get_record",
            identifier=self.user_id,
            tool_input={"object_type": "deals", "record_id": deal_id},
            connection_name="attio",
        )
        return result.data or {}

    def search_companies(self, company_name: str, limit: int = 5) -> list:
        """Search for companies by name."""
        result = self.connect.execute_tool(
            tool_name="attio_search_records",
            identifier=self.user_id,
            tool_input={"object_type": "companies", "query": company_name, "limit": limit},
            connection_name="attio",
        )
        return result.data.get("results", []) if result.data else []
