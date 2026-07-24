"""
Startup provisioning: verify the targeted Salesforce opportunity is findable,
and that the deal room Google Drive doc exists (or can be created).

Honest about the limits of what GOOGLEDRIVE's API actually allows: it can
create a file's metadata (a blank Google Doc) but cannot write real body
content, and there is no way to auto-create a Salesforce opportunity that
doesn't exist -- that's a CRM data problem, not something this agent should
paper over.
"""

import logging

from connectors import ConnectorError, GoogleDriveConnector, SalesforceConnector

logger = logging.getLogger(__name__)


class ProvisioningError(Exception):
    """Raised when required setup is missing and cannot be auto-created."""


def ensure_opportunity_findable(
    salesforce: SalesforceConnector,
    opportunity_id: str,
    opportunity_name: str,
) -> dict:
    """
    Confirm the targeted opportunity exists in Salesforce and return its record.

    Raises ProvisioningError if it can't be found -- this agent cannot create
    Salesforce opportunities on your behalf (that's a sales-process decision,
    not a provisioning gap), so a missing/misconfigured target fails fast with
    a clear message rather than silently syncing an empty deal room.
    """
    try:
        opportunity = salesforce.find_opportunity(opportunity_id, opportunity_name)
    except ConnectorError as e:
        raise ProvisioningError(
            f"Could not query Salesforce for the target opportunity: {e}\n"
            f"Check that SALESFORCE_CONNECTOR matches your exact Scalekit connection "
            f"name and that SALESFORCE_USER has access to Opportunity records."
        ) from e

    if not opportunity:
        target = opportunity_id or opportunity_name
        raise ProvisioningError(
            f"No Salesforce opportunity found matching '{target}'. "
            f"Set OPPORTUNITY_ID to an exact Opportunity Id, or OPPORTUNITY_NAME "
            f"to a name/substring that matches exactly one open opportunity."
        )

    logger.info(f"✓ Opportunity found: '{opportunity.get('Name')}' ({opportunity.get('Id')})")
    return opportunity


def ensure_deal_room_doc(
    drive: GoogleDriveConnector,
    doc_id: str,
    doc_name: str,
    folder_id: str,
    opportunity_name: str,
) -> dict:
    """
    Verify (or create) the Google Drive file that serves as this opportunity's
    deal room. Returns its metadata (at least {"id": ..., "name": ...}).

    If DEAL_ROOM_DOC_ID is set, it must already exist and be accessible --
    this agent does not create a replacement if a configured ID is wrong,
    since that could silently start writing to the wrong file's neighborhood.
    If only DEAL_ROOM_DOC_NAME is set, the agent finds-or-creates a Google Doc
    with that name (metadata only; see connectors.GoogleDriveConnector for why
    the doc's body content can't be populated, and how comments are used instead).
    """
    if doc_id:
        try:
            metadata = drive.get_file_metadata(doc_id)
        except ConnectorError as e:
            raise ProvisioningError(
                f"Cannot access Google Drive file '{doc_id}': {e}\n"
                f"Confirm DEAL_ROOM_DOC_ID is correct and shared with your connected "
                f"Google Drive account, or unset it and set DEAL_ROOM_DOC_NAME instead "
                f"to let the agent find-or-create the doc by name."
            ) from e
        logger.info(f"✓ Deal room doc found: '{metadata.get('name')}' ({doc_id})")
        return metadata

    name = doc_name or f"Deal Room - {opportunity_name}"
    try:
        metadata = drive.find_or_create_deal_room_doc(
            name=name,
            folder_id=folder_id,
            description=f"Deal Room Sync Agent -- {opportunity_name}",
        )
    except ConnectorError as e:
        raise ProvisioningError(
            f"Could not find or create the deal room doc '{name}': {e}\n"
            f"Confirm GOOGLE_DRIVE_CONNECTOR matches your exact Scalekit connection "
            f"name and that GOOGLE_DRIVE_USER has Drive access."
        ) from e

    logger.info(f"✓ Deal room doc ready: '{metadata.get('name')}' ({metadata.get('id')})")
    return metadata
