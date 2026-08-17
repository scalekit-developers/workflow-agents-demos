"""
Normalized hire shape shared by both provisioning modes.

NEW_HIRE_MODE=create (default): one hire's real details come directly from
NEW_HIRE_* env vars, and this agent creates them for real in Deel via
deelmcp_org_direct_employee_create (see connectors.py DeelConnector,
run_flow.py Step 2). Salary/currency/seniority/nationality/state are only
meaningful in this mode, since only this mode actually calls Deel's creation
tool.

NEW_HIRE_MODE=scan: hires are detected FROM Deel instead -- HR creates them
directly in the Deel dashboard (or another process calls create mode), and
this agent scans deelmcp_onboarding_tracker_list for records whose progress
status is still INVITED (see connectors.py DeelConnector.list_onboarding_hires
for why this genuinely works, correcting an earlier finding that Deel had no
such capability). Scan mode never calls the Deel creation tool -- it only
detects existing records and drives Workspace/Notion/Slack for whichever ones
aren't yet fully provisioned. This is why Hire below only carries the fields
common to both origins (name, emails, start date, job title, country); it
deliberately does NOT carry salary/currency/seniority, which the tracker
doesn't expose and which are meaningless once Deel creation is out of scope.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class Hire:
    """One new hire's details, regardless of whether they came from .env config or Deel's onboarding tracker."""

    first_name: str
    last_name: str
    personal_email: str
    work_email: str
    start_date: str
    job_title: str
    country: str
    employment_type: str = "FULL_TIME"
    # Only ever set in create mode -- see module docstring.
    deel_contract_id: str = ""
    deel_employee_id: str = ""
    # Only ever set in scan mode -- a stable Deel-side identity to fingerprint on.
    tracker_unique_id: str = ""
    source: str = "create"  # "create" or "scan"
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


def hire_from_config(cfg) -> Hire:
    """Build a Hire from NEW_HIRE_* config (create mode)."""
    return Hire(
        first_name=cfg.new_hire_first_name,
        last_name=cfg.new_hire_last_name,
        personal_email=cfg.new_hire_personal_email,
        work_email=cfg.new_hire_work_email or cfg.new_hire_personal_email,
        start_date=cfg.new_hire_start_date,
        job_title=cfg.new_hire_job_title,
        country=cfg.new_hire_country,
        employment_type=cfg.new_hire_employment_type,
        source="create",
    )


def hire_from_tracker_record(record: Dict[str, Any]) -> Optional[Hire]:
    """
    Build a Hire from one deelmcp_onboarding_tracker_list record (with
    include_overview=True). Returns None if the record is missing fields
    this agent cannot work without (a name and a start date), logging
    nothing itself -- the caller (run_flow.py) decides how to report a
    skipped record, matching aggregator.py's existing "never raise on
    incomplete source data" pattern from create mode's field handling.
    """
    overview = record.get("overview") or {}
    hris_profile = overview.get("hris_profile") or record.get("hris_profile") or {}
    contract = overview.get("contract") or record.get("contract") or {}
    summary_list = overview.get("summary") or []
    summary = {item.get("type"): item.get("value") for item in summary_list if isinstance(item, dict)}

    full_name = (hris_profile.get("name") or "").strip()
    if not full_name:
        return None
    parts = full_name.split(" ", 1)
    first_name = parts[0]
    last_name = parts[1] if len(parts) > 1 else ""

    start_date_raw = summary.get("START_DATE") or contract.get("effective_date") or ""
    start_date = start_date_raw.split("T")[0] if start_date_raw else ""
    if not start_date:
        return None

    country = ""
    location_data = None
    for item in summary_list:
        if isinstance(item, dict) and item.get("type") == "LOCATION_OF_WORK":
            location_data = item.get("data") or {}
    if location_data:
        country = location_data.get("country", "")

    personal_email = hris_profile.get("email") or ""
    work_email = hris_profile.get("work_email") or personal_email

    return Hire(
        first_name=first_name,
        last_name=last_name,
        personal_email=personal_email,
        work_email=work_email,
        start_date=start_date,
        job_title=summary.get("JOB_TITLE") or "",
        country=country,
        deel_contract_id=contract.get("id", "") or record.get("contract", {}).get("id", ""),
        deel_employee_id=(record.get("hris_profile") or {}).get("oid", ""),
        tracker_unique_id=record.get("unique_id", ""),
        source="scan",
        raw=record,
    )
