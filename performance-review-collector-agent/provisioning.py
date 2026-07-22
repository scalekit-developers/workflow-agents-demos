"""
Startup provisioning: ensure the Airtable table this agent depends on exists,
creating it (with the required fields) if it's missing. Also validates the
Google Form has the structure the agent needs, since Google Forms cannot be
auto-populated with questions via the API (Scalekit's GOOGLEFORMS connector
only exposes create_form/get_form/list_responses/get_response -- no
add-question tool exists).
"""

import logging
from typing import Optional

from connectors import AirtableConnector, ConnectorError, GoogleFormsConnector

logger = logging.getLogger(__name__)

# Fields created on a fresh "Performance Reviews" table. Any additional rating
# columns you add later (matching /rating|score/i) are picked up automatically
# by the aggregator -- these are just a sensible starting schema.
_DEFAULT_FIELDS = [
    {"name": "Employee Name", "type": "singleLineText"},
    {"name": "Manager Email", "type": "singleLineText"},
    {"name": "Communication Rating", "type": "number", "options": {"precision": 0}},
    {"name": "Impact Rating", "type": "number", "options": {"precision": 0}},
    {"name": "Comments", "type": "multilineText"},
]


class ProvisioningError(Exception):
    """Raised when required setup is missing and cannot be auto-created."""


def ensure_airtable_table(
    airtable: AirtableConnector,
    base_id: str,
    table_name: str,
    employee_field: str,
    manager_field: str,
) -> None:
    """
    Ensure `table_name` exists in `base_id` with at least the employee/manager
    fields the agent requires. Creates the table with a default schema if it's
    missing. Raises ProvisioningError if the base itself doesn't exist or isn't
    accessible -- Airtable's API cannot create a new base, only tables within one.
    """
    try:
        schema = airtable.execute_tool("airtable_get_base_schema", base_id=base_id) or {}
    except ConnectorError as e:
        raise ProvisioningError(
            f"Cannot access Airtable base '{base_id}': {e}\n"
            f"Airtable's API cannot create a new base -- create an empty base at "
            f"airtable.com first, then set AIRTABLE_BASE_ID to its ID."
        ) from e

    existing_tables = {t.get("name") for t in schema.get("tables", [])}

    if table_name in existing_tables:
        logger.info(f"✓ Airtable table '{table_name}' already exists")
        _ensure_required_fields(airtable, base_id, table_name, schema, employee_field, manager_field)
        return

    logger.warning(f"Airtable table '{table_name}' not found -- creating it now")
    fields = list(_DEFAULT_FIELDS)
    field_names = {f["name"] for f in fields}
    if employee_field not in field_names:
        fields.insert(0, {"name": employee_field, "type": "singleLineText"})
    if manager_field not in field_names:
        fields.insert(1, {"name": manager_field, "type": "singleLineText"})

    try:
        airtable.execute_tool(
            "airtable_create_table",
            base_id=base_id,
            name=table_name,
            fields=fields,
        )
        logger.info(f"✓ Created Airtable table '{table_name}' with default review fields")
    except ConnectorError as e:
        raise ProvisioningError(f"Failed to create Airtable table '{table_name}': {e}") from e


def _ensure_required_fields(
    airtable: AirtableConnector,
    base_id: str,
    table_name: str,
    schema: dict,
    employee_field: str,
    manager_field: str,
) -> None:
    """Warn (don't fail) if an existing table is missing the two fields the agent relies on."""
    table = next((t for t in schema.get("tables", []) if t.get("name") == table_name), None)
    if not table:
        return

    existing_fields = {f.get("name") for f in table.get("fields", [])}
    for required in (employee_field, manager_field):
        if required not in existing_fields:
            logger.warning(
                f"Airtable table '{table_name}' is missing expected field '{required}' -- "
                f"add it manually or direct-report resolution will find no records"
            )


def validate_google_form(forms: GoogleFormsConnector, form_id: str, employee_question_id: str) -> None:
    """
    Validate the configured Google Form exists and (if configured) contains the
    employee-identifying question. Cannot auto-create form questions -- the
    GOOGLEFORMS connector has no add-question tool -- so this fails fast with
    clear setup instructions instead of silently proceeding against a broken form.
    """
    try:
        form = forms.get_form(form_id)
    except ConnectorError as e:
        raise ProvisioningError(
            f"Cannot access Google Form '{form_id}': {e}\n"
            f"Create the form manually at forms.google.com with a question asking "
            f"'Which employee is this feedback about?', then set GOOGLE_FORM_ID."
        ) from e

    items = form.get("items", [])
    if not items:
        logger.warning(
            f"Google Form '{form_id}' has no questions yet. Google Forms cannot be "
            f"populated with questions via API -- add them manually at forms.google.com."
        )
        return

    if employee_question_id:
        question_ids = {
            item.get("questionItem", {}).get("question", {}).get("questionId")
            for item in items
        }
        if employee_question_id not in question_ids:
            logger.warning(
                f"FORM_EMPLOYEE_QUESTION_ID '{employee_question_id}' not found in form "
                f"'{form_id}' -- check googleforms_get_form output and update your .env"
            )

    logger.info(f"✓ Google Form '{form_id}' is accessible ({len(items)} question(s))")
