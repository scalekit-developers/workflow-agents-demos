"""
Business logic: change-eligibility hard gate, new-value validation, PII
masking, and message building for the payroll/bank-detail change pipeline.

This module deliberately contains NO Scalekit calls -- it is pure, testable
logic that operates on data already fetched by connectors.py, and it never
receives or returns a Gusto/Slack/Sheets tool_input/response shape directly
(run_flow.py is the only place that wires aggregator functions to connector
calls). This mirrors the reference repos' separation between "aggregator.py:
pure in-process logic" and "connectors.py: the only place actions.execute_tool
is called".

Eligibility is a hard, business-logic gate distinct from provisioning.py's
"is Gusto/Sheets reachable at all" checks: eligibility asks "should THIS
specific employee's THIS specific requested change be allowed to proceed",
and a failure here must STOP the pipeline before any write is attempted (see
run_flow.py Step 1). Malformed new-value validation is also treated as part
of the same hard gate (a routing number that fails a checksum, or an account
number of an implausible length, is exactly as disqualifying as an inactive
employment status): both are checked before Step 2, and both produce the
same class of "refuse to submit" outcome.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

# Employment/onboarding states that Gusto exposes on employee and contractor
# records which we treat as disqualifying for a payroll/bank-detail change.
# A person mid-termination or not yet onboarded should not have their payroll
# destination changed until that process resolves, regardless of what field
# they're trying to change.
_DISQUALIFYING_ONBOARDING_STATUSES = {"admin_onboarding_review", "onboarding_incomplete"}


@dataclass
class EligibilityResult:
    """
    Outcome of the Step 1 eligibility hard gate. `eligible=False` must always
    carry at least one human-readable reason -- run_flow.py logs every reason
    loudly (ERROR level) and refuses to proceed to submission if this is not
    eligible, with no silent-skip path.
    """

    eligible: bool
    reasons: List[str]
    record_uuid: Optional[str] = None
    record_type: Optional[str] = None  # "employee" or "contractor"

    def reason_summary(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "no reason recorded"


def check_employee_eligibility(record: Optional[dict], record_type: str) -> EligibilityResult:
    """
    Hard gate: evaluate whether the given Gusto employee/contractor record is
    eligible for a payroll/bank-detail change to proceed.

    This function ONLY evaluates the record itself (does it exist, is it
    active, is onboarding complete, is it not mid-termination). New-value
    format validation (routing number checksum, account number shape) is a
    separate, equally-hard gate performed by validate_new_value() -- kept
    separate because a record can be perfectly eligible while the requested
    new value is malformed, and vice versa, and run_flow.py needs to be able
    to report which of the two failed.

    Returns EligibilityResult(eligible=False, ...) -- never raises -- for
    every disqualifying condition, so run_flow.py has one uniform place to
    check before proceeding. An inconclusive result (record present but
    missing the fields needed to evaluate a rule) is also treated as
    NOT eligible: per the safety requirement for this agent, an eligibility
    check that cannot conclusively pass must never be treated as a pass.
    """
    reasons: List[str] = []

    if record is None:
        return EligibilityResult(
            eligible=False,
            reasons=[f"No matching {record_type} record found in Gusto for this employee"],
        )

    record_uuid = record.get("uuid")
    if not record_uuid:
        reasons.append(f"Gusto {record_type} record has no uuid field -- cannot proceed inconclusively")

    is_active = record.get("is_active")
    if is_active is None:
        reasons.append("Gusto record does not report is_active -- eligibility is inconclusive")
    elif is_active is False:
        reasons.append("Employee/contractor is not active in Gusto (is_active=false)")

    if record.get("dismissal_date"):
        reasons.append(f"Employee has a dismissal_date on file ({record.get('dismissal_date')}) -- termination in progress")

    onboarding_status = record.get("onboarding_status")
    if onboarding_status in _DISQUALIFYING_ONBOARDING_STATUSES:
        reasons.append(f"Onboarding status is '{onboarding_status}', which blocks payroll changes")
    elif onboarding_status is None:
        reasons.append("Gusto record does not report onboarding_status -- eligibility is inconclusive")

    if record.get("upcoming_employment"):
        reasons.append("Employee has an upcoming_employment change scheduled -- treat as a lock/cooldown window")

    if reasons:
        return EligibilityResult(eligible=False, reasons=reasons, record_uuid=record_uuid, record_type=record_type)

    return EligibilityResult(eligible=True, reasons=[], record_uuid=record_uuid, record_type=record_type)


def validate_new_value(change_type: str, new_value: str) -> EligibilityResult:
    """
    Hard gate: validate that new_value is well-formed for change_type BEFORE
    any submission is attempted. Never logs new_value itself (callers must
    not pass it to logger; only this function's boolean/reason result is
    logged).
    """
    reasons: List[str] = []
    change_type = (change_type or "").strip().lower()
    value = (new_value or "").strip()

    if not value:
        return EligibilityResult(eligible=False, reasons=["New value is empty"])

    if change_type == "routing_number":
        if not value.isdigit() or len(value) != 9:
            reasons.append("Routing number must be exactly 9 digits")
        elif not _is_valid_aba_checksum(value):
            reasons.append("Routing number fails the standard ABA checksum validation")
    elif change_type == "bank_account":
        if not value.isdigit():
            reasons.append("Bank account number must be numeric")
        elif not (4 <= len(value) <= 17):
            reasons.append("Bank account number must be 4-17 digits (outside plausible US range)")
    elif change_type in ("pay_rate", "compensation"):
        try:
            amount = float(value)
            if amount <= 0:
                reasons.append("Pay rate/compensation amount must be positive")
        except ValueError:
            reasons.append("Pay rate/compensation must be a numeric amount")
    else:
        reasons.append(f"Unknown CHANGE_TYPE '{change_type}' -- no validation rule defined, treated as invalid")

    if reasons:
        return EligibilityResult(eligible=False, reasons=reasons)
    return EligibilityResult(eligible=True, reasons=[])


def _is_valid_aba_checksum(routing_number: str) -> bool:
    """
    Standard ABA routing-number checksum: 3*(d1+d4+d7) + 7*(d2+d5+d8) +
    1*(d3+d6+d9) must be divisible by 10. Applied only after confirming the
    value is exactly 9 digits.
    """
    digits = [int(c) for c in routing_number]
    checksum = (
        3 * (digits[0] + digits[3] + digits[6])
        + 7 * (digits[1] + digits[4] + digits[7])
        + 1 * (digits[2] + digits[5] + digits[8])
    )
    return checksum % 10 == 0


def mask_value(change_type: str, new_value: str) -> str:
    """
    Return a masked, log-safe/Sheets-safe/Slack-safe representation of a
    sensitive new value. NEVER returns the plaintext value. Only the last 4
    characters are ever shown, matching common financial-industry masking
    convention (e.g. how card networks and banks display "ending in 1234").
    """
    value = (new_value or "").strip()
    if len(value) <= 4:
        return "****" + value  # still shows all digits if very short; acceptable, matches convention
    return "*" * (len(value) - 4) + value[-4:]


def describe_change(change_type: str, masked_value: str) -> str:
    """Human-readable, masked description of a change, used in both the Sheets log row and the Slack DM."""
    labels = {
        "bank_account": "direct deposit bank account number",
        "routing_number": "direct deposit routing number",
        "pay_rate": "pay rate",
        "compensation": "compensation",
    }
    label = labels.get(change_type, change_type)
    return f"{label} updated, ending in {masked_value[-4:]}"


def build_slack_confirmation(employee_name: str, change_type: str, masked_value: str, submitted_at: str) -> str:
    """Build the masked Slack DM text sent to the employee after a successful change."""
    description = describe_change(change_type, masked_value)
    return (
        f"Hi {employee_name}, your payroll change request has been processed.\n\n"
        f"*Change:* {description}\n"
        f"*Processed at:* {submitted_at}\n\n"
        f"If you did not request this change, contact People Ops immediately."
    )


def build_sheets_row(
    run_date: str,
    employee_email: str,
    change_type: str,
    masked_value: str,
    status: str,
    detail: str,
) -> list:
    """
    Build a single audit-log row for Google Sheets. `detail` must already be
    masked/redacted by the caller (never the raw new_value or a raw Gusto
    rejection message that might echo the submitted value back).
    """
    return [run_date, employee_email, change_type, masked_value, status, detail]
