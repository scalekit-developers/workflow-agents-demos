"""
Aggregation logic: merge Airtable structured ratings and Google Forms free-text
feedback into one feedback bundle per employee, scoped to a manager's direct reports.
"""

import logging
import re
from statistics import mean
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Airtable fields that hold numeric ratings (1-5 style scores).
# Any field matching this pattern is treated as a rating, so the agent works
# across differently-named review templates without hardcoding column names.
_RATING_FIELD_PATTERN = re.compile(r"(rating|score)", re.IGNORECASE)


class EmployeeFeedback:
    """All feedback collected for a single employee in this review cycle."""

    def __init__(self, name: str):
        self.name = name
        self.ratings: Dict[str, List[float]] = {}
        self.airtable_comments: List[str] = []
        self.form_comments: List[str] = []

    def add_airtable_record(self, fields: Dict) -> None:
        for field_name, value in fields.items():
            if _RATING_FIELD_PATTERN.search(field_name) and isinstance(value, (int, float)):
                self.ratings.setdefault(field_name, []).append(float(value))
            elif isinstance(value, str) and field_name.lower() in (
                "comments", "comment", "feedback", "notes"
            ):
                text = value.strip()
                if text:
                    self.airtable_comments.append(text)

    def add_form_comment(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self.form_comments.append(text)

    def average_ratings(self) -> Dict[str, float]:
        return {
            field: round(mean(values), 2)
            for field, values in self.ratings.items()
            if values
        }

    def overall_average(self) -> Optional[float]:
        averages = self.average_ratings()
        if not averages:
            return None
        return round(mean(averages.values()), 2)

    def all_comments(self) -> List[str]:
        return self.airtable_comments + self.form_comments

    def response_count(self) -> int:
        rating_responses = max((len(v) for v in self.ratings.values()), default=0)
        return rating_responses + len(self.form_comments)

    def has_feedback(self) -> bool:
        return bool(self.ratings) or bool(self.all_comments())


def resolve_direct_reports(
    airtable_records: List[Dict],
    manager_field: str,
    employee_field: str,
    manager_email: str,
    configured_reports: Optional[List[str]],
) -> List[str]:
    """
    Determine which employees are in scope for this manager.

    Prefers the Airtable Manager field (source of truth per review record).
    Falls back to the configured DIRECT_REPORTS list if no record names the manager
    (e.g. the field is empty or the manager hasn't been assigned reviews yet).
    """
    from_airtable = set()
    for record in airtable_records:
        fields = record.get("fields", {})
        record_manager = str(fields.get(manager_field, "")).strip().lower()
        if record_manager == manager_email.strip().lower():
            employee = fields.get(employee_field)
            if employee:
                from_airtable.add(employee)

    if from_airtable:
        return sorted(from_airtable)

    if configured_reports:
        logger.warning(
            f"No Airtable records tagged with manager '{manager_email}' -- "
            f"falling back to DIRECT_REPORTS env list"
        )
        return configured_reports

    logger.warning(f"No direct reports found for manager '{manager_email}' in Airtable or config")
    return []


def build_employee_feedback(
    direct_reports: List[str],
    airtable_records: List[Dict],
    employee_field: str,
    form_responses: List[Dict],
    form_employee_question_id: str,
) -> Dict[str, EmployeeFeedback]:
    """Group Airtable records and Google Forms responses by employee name."""
    bundles = {name: EmployeeFeedback(name) for name in direct_reports}
    reports_set = set(direct_reports)

    for record in airtable_records:
        fields = record.get("fields", {})
        employee = fields.get(employee_field)
        if employee in reports_set:
            bundles[employee].add_airtable_record(fields)

    for response in form_responses:
        answers = response.get("answers", {})
        employee = _extract_form_employee(answers, form_employee_question_id)
        if employee not in reports_set:
            continue
        for question_id, answer in answers.items():
            if question_id == form_employee_question_id:
                continue
            text = _extract_answer_text(answer)
            if text:
                bundles[employee].add_form_comment(text)

    return bundles


def _extract_form_employee(answers: Dict, employee_question_id: str) -> Optional[str]:
    """Pull the employee name out of a form response's answers dict."""
    if employee_question_id and employee_question_id in answers:
        return _extract_answer_text(answers[employee_question_id])

    # Fallback: no question ID configured -- look for a short single-choice-like
    # answer among the response (best-effort, used only when FORM_EMPLOYEE_QUESTION_ID unset).
    for answer in answers.values():
        text = _extract_answer_text(answer)
        if text and len(text.split()) <= 4:
            return text
    return None


def _extract_answer_text(answer: Dict) -> str:
    """Google Forms answers are nested under textAnswers.answers[].value."""
    text_answers = (answer or {}).get("textAnswers", {}).get("answers", [])
    return " ".join(a.get("value", "") for a in text_answers).strip()
