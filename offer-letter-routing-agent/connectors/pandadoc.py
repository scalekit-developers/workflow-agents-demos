"""PandaDoc connector — create, send, and track offer documents.

Parameter shapes below were verified against PandaDoc's live MCP server
(not just Scalekit's catalog schema, which differs from what the upstream
server actually accepts):
  - documents_create expects the payload nested under "request", with a
    "source" discriminator ("template" for template-based creation).
  - documents_create's recipient "role" must match a role name that actually
    exists on the template (PandaDoc's own default is "Client", not "Signer").
  - documents_send, documents_status_get, and documents_details_get all key
    the document by "document_id", not "id".
"""
import logging
from typing import Any, Optional

logger = logging.getLogger("offer-letter-agent")

DEFAULT_RECIPIENT_ROLE = "Client"


class PandaDocConnector:
    """Offer document lifecycle: create, send, check status."""

    def __init__(self, connect: Any, user_id: str, connection_name: str = "pandadocmcp"):
        """Initialize with Scalekit connect client, user ID, and connection name.

        connection_name is passed through to every execute_tool call — without
        it, Scalekit disambiguates by identifier alone, which fails with
        "multiple connected accounts found" if that identifier happens to be
        registered under more than one connection for this connector.
        """
        self.connect = connect
        self.user_id = user_id
        self.connection_name = connection_name

    def create_from_template(
        self,
        template_uuid: str,
        name: str,
        candidate_email: str,
        candidate_first_name: str,
        candidate_last_name: str,
        tokens: dict[str, str],
        recipient_role: str = DEFAULT_RECIPIENT_ROLE,
    ) -> Optional[dict]:
        """Create a new offer document from an existing PandaDoc template.

        `tokens` fills template variables like {{base_salary}}, {{role_title}}, {{start_date}}
        — these must match the token names configured in the PandaDoc template.
        `recipient_role` must match a recipient role name that actually exists
        on the template (check in PandaDoc's template editor — "Client" is
        PandaDoc's own default role name, not a placeholder).
        """
        try:
            result = self.connect.execute_tool(
                tool_name="pandadocmcp_documents_create",
                identifier=self.user_id,
                connection_name=self.connection_name,
                tool_input={
                    "request": {
                        "source": "template",
                        "template_uuid": template_uuid,
                        "name": name,
                        "recipients": [
                            {
                                "email": candidate_email,
                                "first_name": candidate_first_name,
                                "last_name": candidate_last_name,
                                "role": recipient_role,
                            }
                        ],
                        "tokens": [{"name": k, "value": v} for k, v in tokens.items()],
                    }
                },
            )
            data = result.data or {}
            error = (data.get("structuredContent") or {}).get("error") if isinstance(data, dict) else None
            if error:
                logger.error(f"PandaDoc rejected document creation: {error.get('detail') or error.get('message')}")
                return None
            doc_id = data.get("id")
            if doc_id:
                logger.info(f"Created offer doc from template: {name} (id={doc_id})")
            return data
        except Exception as e:
            logger.error(f"Failed to create document from template: {e}")
            return None

    def send(self, document_id: str, message: str, subject: str) -> bool:
        """Send a draft document to its recipients for review and signature."""
        try:
            result = self.connect.execute_tool(
                tool_name="pandadocmcp_documents_send",
                identifier=self.user_id,
                connection_name=self.connection_name,
                tool_input={
                    "document_id": document_id,
                    "message": message,
                    "subject": subject,
                },
            )
            data = result.data or {}
            error = (data.get("structuredContent") or {}).get("error") if isinstance(data, dict) else None
            if error:
                logger.error(f"PandaDoc rejected send: {error.get('detail') or error.get('message')}")
                return False
            logger.info(f"Sent document {document_id} to recipients")
            return True
        except Exception as e:
            logger.error(f"Failed to send document {document_id}: {e}")
            return False

    def get_status(self, document_id: str) -> Optional[str]:
        """Get the current status of a document (Draft, Sent, Completed, etc.)."""
        try:
            result = self.connect.execute_tool(
                tool_name="pandadocmcp_documents_status_get",
                identifier=self.user_id,
                connection_name=self.connection_name,
                tool_input={"document_id": document_id},
            )
            status = (result.data or {}).get("status")
            logger.debug(f"Document {document_id} status: {status}")
            return status
        except Exception as e:
            logger.error(f"Failed to get status for {document_id}: {e}")
            return None

    def get_details(self, document_id: str) -> Optional[dict]:
        """Get full details for a document, including recipients and fields."""
        try:
            result = self.connect.execute_tool(
                tool_name="pandadocmcp_documents_details_get",
                identifier=self.user_id,
                connection_name=self.connection_name,
                tool_input={"document_id": document_id},
            )
            return result.data or {}
        except Exception as e:
            logger.error(f"Failed to get details for {document_id}: {e}")
            return None
