"""Gong connector for fetching sales calls."""


class GongConnector:
    """Fetch calls from Gong."""

    def __init__(self, connect, user_id: str):
        self.connect = connect
        self.user_id = user_id

    def list_calls(self, limit: int = 10) -> list:
        """Fetch recent calls from Gong."""
        result = self.connect.execute_tool(
            tool_name="gong_list_calls",
            identifier=self.user_id,
            tool_input={"limit": limit},
            connection_name="gong",
        )
        return result.data.get("calls", []) if result.data else []

    def get_call_details(self, call_id: str) -> dict:
        """Get detailed information for a call."""
        result = self.connect.execute_tool(
            tool_name="gong_get_call",
            identifier=self.user_id,
            tool_input={"call_id": call_id},
            connection_name="gong",
        )
        return result.data or {}
