"""
Startup provisioning: ensure the Google Sheets destination tab exists inside
the configured spreadsheet, creating it if missing, and validate the HubSpot
deal pipeline/stage IDs needed for stage-label resolution are actually
resolvable before pulling any pipeline data.

Google Sheets has a googlesheets_create_spreadsheet tool (verified live), but
this agent's default/documented flow does not depend on it: creating a brand
new spreadsheet on every run (or worse, on every misconfiguration) is not
idempotent and would scatter forecast history across many spreadsheets. The
supported flow is the same as the reference repo's Airtable-base constraint:
you create ONE spreadsheet manually (or once, via
googlesheets_create_spreadsheet, as this repo's own build/test process did),
put its ID in GOOGLE_SHEETS_SPREADSHEET_ID, and this module auto-creates/
manages the TAB within it on every run.
"""

import logging

from connectors import ConnectorError, GoogleSheetsConnector, HubSpotConnector

logger = logging.getLogger(__name__)


class ProvisioningError(Exception):
    """Raised when required setup is missing and cannot be auto-created."""


def ensure_google_sheet_tab(sheets: GoogleSheetsConnector, spreadsheet_id: str, tab_name: str) -> None:
    """
    Ensure `tab_name` exists as a sheet/tab inside `spreadsheet_id`, creating
    it (with a header row) if missing. Raises ProvisioningError if the
    spreadsheet itself doesn't exist or isn't accessible.
    """
    try:
        created = sheets.ensure_tab(spreadsheet_id, tab_name)
    except ConnectorError as e:
        raise ProvisioningError(
            f"Cannot access Google Sheets spreadsheet '{spreadsheet_id}': {e}\n"
            f"Create an empty spreadsheet at sheets.google.com first (or run "
            f"googlesheets_create_spreadsheet once), share it with your "
            f"connected Google account, then set GOOGLE_SHEETS_SPREADSHEET_ID "
            f"to its ID from the URL."
        ) from e

    if created:
        logger.warning(f"Google Sheets tab '{tab_name}' not found -- created it now")
    else:
        logger.info(f"[OK] Google Sheets tab '{tab_name}' already exists")

    header = [
        "Run Date", "Analyst", "Forecast Period", "Stage", "Source",
        "Open Count", "Open Value", "Coverage Ratio", "At Risk",
    ]
    try:
        sheets.append_header_if_empty(spreadsheet_id, tab_name, header)
    except ConnectorError as e:
        logger.warning(f"Could not verify/write header row for '{tab_name}': {e}")


def resolve_hubspot_open_stages(hubspot: HubSpotConnector) -> dict:
    """
    Fetch HubSpot's deal pipelines and return {stage_id: stage_label} for
    every stage across every pipeline whose metadata.isClosed is "false".
    Raises ProvisioningError if the pipeline list can't be fetched at all --
    without it, no HubSpot deal's stage ID can be resolved to a human label
    or classified as open vs. closed.
    """
    try:
        pipelines = hubspot.list_deal_pipelines()
    except ConnectorError as e:
        raise ProvisioningError(
            f"Cannot fetch HubSpot deal pipelines: {e}\n"
            f"Confirm HUBSPOT_CONNECTOR points at an ACTIVE HubSpot connection "
            f"with CRM read scopes."
        ) from e

    if not pipelines:
        logger.warning("HubSpot returned zero deal pipelines -- HubSpot pipeline data will be empty this cycle")
        return {}

    open_stage_labels = {}
    for pipeline in pipelines:
        for stage in pipeline.get("stages", []):
            stage_id = stage.get("id")
            label = stage.get("label", stage_id)
            is_closed = str(stage.get("metadata", {}).get("isClosed", "false")).lower() == "true"
            if stage_id and not is_closed:
                open_stage_labels[stage_id] = label

    logger.info(
        f"[OK] Resolved {len(open_stage_labels)} open HubSpot stage(s) across "
        f"{len(pipelines)} pipeline(s)"
    )
    return open_stage_labels
