"""Input validation for offer requests — fails loud on bad comp/candidate data."""
import re
from datetime import date, datetime
from dataclasses import dataclass

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SALARY_RE = re.compile(r"^\$?[\d,]+(\.\d{2})?$")


@dataclass
class OfferRequest:
    candidate_first_name: str
    candidate_last_name: str
    candidate_email: str
    role_title: str
    base_salary: str
    start_date: str


class ValidationError(Exception):
    """Raised when an offer request fails validation."""


def validate_offer_request(raw: dict) -> OfferRequest:
    """Validate and normalize a raw offer request dict. Raises ValidationError on failure."""
    errors = []

    first_name = (raw.get("candidate_first_name") or "").strip()
    last_name = (raw.get("candidate_last_name") or "").strip()
    email = (raw.get("candidate_email") or "").strip()
    role_title = (raw.get("role_title") or "").strip()
    base_salary = (raw.get("base_salary") or "").strip()
    start_date = (raw.get("start_date") or "").strip()

    if not first_name:
        errors.append("candidate_first_name is required")
    if not last_name:
        errors.append("candidate_last_name is required")

    if not email:
        errors.append("candidate_email is required")
    elif not _EMAIL_RE.match(email):
        errors.append(f"candidate_email is not a valid email address: {email!r}")

    if not role_title:
        errors.append("role_title is required")

    if not base_salary:
        errors.append("base_salary is required")
    elif not _SALARY_RE.match(base_salary.replace("k", "000").replace("K", "000")):
        errors.append(
            f"base_salary must be a plain number like '180000' or '$180,000': {base_salary!r}"
        )

    if not start_date:
        errors.append("start_date is required")
    else:
        parsed = _parse_date(start_date)
        if parsed is None:
            errors.append(
                f"start_date must be in YYYY-MM-DD format: {start_date!r}"
            )
        elif parsed < date.today():
            errors.append(f"start_date {start_date!r} is in the past")

    if errors:
        raise ValidationError("; ".join(errors))

    return OfferRequest(
        candidate_first_name=first_name,
        candidate_last_name=last_name,
        candidate_email=email,
        role_title=role_title,
        base_salary=_normalize_salary(base_salary),
        start_date=start_date,
    )


def _parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _normalize_salary(raw: str) -> str:
    """Normalize salary strings like '180k' or '180000' into '$180,000'."""
    cleaned = raw.strip()
    multiplier = 1
    if cleaned.lower().endswith("k"):
        cleaned = cleaned[:-1]
        multiplier = 1000

    digits = re.sub(r"[^\d.]", "", cleaned)
    if not digits:
        return raw

    amount = float(digits) * multiplier
    if amount == int(amount):
        return f"${amount:,.0f}"
    return f"${amount:,.2f}"
