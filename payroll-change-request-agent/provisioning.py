"""
Startup provisioning: validate that the employee is findable in Gusto and
that the Google Sheets destination tab exists (creating it if missing).

This module answers "does the setup/infrastructure work at all" -- it is
deliberately kept separate from the business-logic eligibility gate in
aggregator.py's check_employee_eligibility(), which answers "should THIS
change be allowed to proceed for THIS employee". A provisioning failure
(employee not findable at all, spreadsheet unreachable) is a setup problem
the operator must fix (wrong email, wrong spreadsheet ID, connector not
authorized). An eligibility failure (employee found, but inactive / mid
termination / onboarding incomplete) is a business decision the agent must
respect, not a setup problem -- see run_flow.py Step 1 for where that gate
lives and why it is checked separately, after provisioning succeeds.

Google Sheets has a googlesheets_create_spreadsheet tool (verified live in
the sibling revenue-forecast-commentary-agent repo), but this agent's
default/documented flow does not depend on it: creating a brand new
spreadsheet on every run (or worse, on every misconfiguration) is not
idempotent and would scatter payroll-change history across many
spreadsheets, which is especially undesirable for an audit log of sensitive
changes. The supported flow is: you create ONE spreadsheet manually, put its
ID in GOOGLE_SHEETS_SPREADSHEET_ID, and this module auto-creates/manages the
TAB within it on every run.
"""

import logging
from typing import Optional, Tuple

from connectors import ConnectorError, GoogleSheetsConnector, GustoConnector

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

    # Header intentionally has no column for the raw new value -- only a
    # masked value is ever written (see aggregator.py mask_value()).
    header = ["Run Date", "Employee Email", "Change Type", "Masked Value", "Status", "Detail"]
    try:
        sheets.append_header_if_empty(spreadsheet_id, tab_name, header)
    except ConnectorError as e:
        logger.warning(f"Could not verify/write header row for '{tab_name}': {e}")


def find_employee_record(gusto: GustoConnector, employee_email: str, record_type_hint: str = "") -> Tuple[Optional[dict], str]:
    """
    Find the employee/contractor record in Gusto for employee_email. Tries
    the hinted record_type first if given (EMPLOYEE_RECORD_TYPE), otherwise
    tries employee (W-2) first, then falls back to contractor -- Gusto
    companies can be employee-only, contractor-only, or mixed, and this
    workspace's live company ("Infrasity") is provisioned contractor_only,
    which is exactly the case this fallback exists for.

    Returns (record_or_none, record_type_used). Raises ProvisioningError only
    if the Gusto lookup itself fails (connector error) -- a clean "not found"
    result is NOT a ProvisioningError, since a missing record is exactly the
    scenario the eligibility gate in aggregator.py is designed to catch and
    report loudly (see run_flow.py Step 1). Keeping "lookup failed" and
    "lookup succeeded but found nothing" as distinct outcomes here lets
    run_flow.py give an accurate, specific error message for each.
    """
    try:
        if record_type_hint == "contractor":
            record = gusto.find_contractor_by_email(employee_email)
            return record, "contractor"
        if record_type_hint == "employee":
            record = gusto.find_employee_by_email(employee_email)
            return record, "employee"

        record = gusto.find_employee_by_email(employee_email)
        if record is not None:
            return record, "employee"
        record = gusto.find_contractor_by_email(employee_email)
        return record, "contractor"
    except ConnectorError as e:
        raise ProvisioningError(
            f"Could not query Gusto for employee '{employee_email}': {e}\n"
            f"Confirm GUSTO_CONNECTOR points at an ACTIVE Gusto connection "
            f"with employees:read/contractors:read scope."
        ) from e
