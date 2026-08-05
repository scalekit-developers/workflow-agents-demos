"""
State management for idempotency on PTO submissions, and a local leave-usage
ledger used as a stand-in for Gusto's missing balance API.

A PTO request is fundamentally different from the recurring/polling
workloads in the sibling agents (revenue-forecast-commentary-agent,
performance-review-collector-agent): there is exactly one leave request to
process per invocation, not an open-ended stream of changing source data.
Re-running the agent with the same employee + date range + PTO type is
almost always an accident (a retried cron job, a re-run after a transient
network error) rather than an intentional second request for the exact same
dates, so the guard here is a straightforward idempotency key, not a
content-drift fingerprint like the revenue-forecast agent's stage-aggregate
comparison.

Fingerprint = sha256(employee_email + pto_type + start_date + end_date),
normalized (lowercased email, ISO dates). Two requests with the same key are
treated as the same request: if the first one already completed the Google
Calendar block and Slack notification, the second run skips re-doing that
work rather than creating a duplicate calendar event or double-DMing the
manager. This also makes "submit once" and "process a small pending queue"
the same underlying operation: each entry in the request ledger is one
request, keyed by its own fingerprint, so POLLING_MODE (see run_flow.py) can
safely re-check the same request on an interval without side effects once
it's marked complete.

Two separate on-disk files are used so the two concerns never collide:
  - state/processed_requests.json: one entry per request fingerprint
    (idempotency ledger, see StateManager below).
  - state/pto_usage.json: one running total of days used per employee this
    year (the balance ledger, see UsageLedger below), which exists only
    because GUSTOMCP has no time-off-balance tool to read from instead (see
    aggregator.py and README).
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def compute_request_fingerprint(employee_email: str, pto_type: str, start_date: str, end_date: str) -> str:
    """
    Build a stable idempotency key for one leave request.

    Unlike the revenue-forecast agent's aggregate content fingerprint (which
    intentionally changes as underlying data drifts, to detect real
    movement), this fingerprint is keyed on the request's identity, not its
    content, since the "content" of a PTO request (who, what type, which
    dates) IS its identity -- there is nothing else to compare against.
    """
    payload = {
        "employee_email": employee_email.strip().lower(),
        "pto_type": pto_type.strip().lower(),
        "start_date": start_date,
        "end_date": end_date,
    }
    blob = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class StateManager:
    """
    Tracks the outcome of each distinct PTO request (by fingerprint) so a
    re-run of the same request doesn't re-block the calendar or re-send the
    Slack DM for work that already completed.
    """

    def __init__(self, state_file: Optional[Path] = None):
        if state_file is None:
            state_file = Path(__file__).parent / "state" / "processed_requests.json"
        self.state_file = state_file
        self._requests: Dict[str, Dict] = {}
        self.load()

    def load(self) -> None:
        if self.state_file.exists():
            try:
                raw = json.loads(self.state_file.read_text())
                self._requests = raw if isinstance(raw, dict) else {}
                logger.debug(f"Loaded state for {len(self._requests)} PTO request(s)")
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError, FileNotFoundError):
                logger.warning("State file corrupted or unreadable, starting fresh")
                self._requests = {}
        else:
            logger.debug("No state file found, starting fresh")
            self._requests = {}

    def save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._requests, indent=2, sort_keys=True))
        tmp.replace(self.state_file)  # atomic on POSIX

    def get(self, fingerprint: str) -> Optional[Dict]:
        return self._requests.get(fingerprint)

    def is_completed(self, fingerprint: str) -> bool:
        """True if this exact request (same employee/type/dates) already fully succeeded."""
        entry = self._requests.get(fingerprint)
        return bool(entry and entry.get("status") == "completed")

    def mark_step(self, fingerprint: str, **fields) -> None:
        """
        Record progress for a request as steps complete, so a crash mid-flow
        leaves an accurate partial record rather than silently losing which
        steps actually ran (e.g. calendar block succeeded but Slack failed).
        """
        entry = self._requests.setdefault(fingerprint, {})
        entry.update(fields)
        self.save()

    def mark_completed(self, fingerprint: str, **fields) -> None:
        self.mark_step(fingerprint, status="completed", **fields)


class UsageLedger:
    """
    A local running total of leave days used per employee, standing in for
    Gusto's missing time-off-balance API (see module docstring). Each
    completed, non-rejected request adds its business-day count here; the
    total is what aggregator.py's validate_leave_request() checks against
    PTO_ANNUAL_ENTITLEMENT_DAYS.

    This is a real limitation, not an implementation detail to gloss over:
    if the same employee's PTO is also tracked natively inside Gusto by some
    other system or process, this ledger will drift out of sync with it,
    since GUSTOMCP exposes no tool this agent can use to read or write an
    authoritative balance. See README's Error Handling & Edge Cases section.
    """

    def __init__(self, usage_file: Optional[Path] = None):
        if usage_file is None:
            usage_file = Path(__file__).parent / "state" / "pto_usage.json"
        self.usage_file = usage_file
        self._usage: Dict[str, float] = {}
        self.load()

    def load(self) -> None:
        if self.usage_file.exists():
            try:
                raw = json.loads(self.usage_file.read_text())
                self._usage = raw if isinstance(raw, dict) else {}
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError, FileNotFoundError):
                logger.warning("PTO usage ledger corrupted or unreadable, starting fresh")
                self._usage = {}
        else:
            self._usage = {}

    def save(self) -> None:
        self.usage_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.usage_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._usage, indent=2, sort_keys=True))
        tmp.replace(self.usage_file)

    def days_used(self, employee_email: str) -> float:
        return float(self._usage.get(employee_email.strip().lower(), 0.0))

    def add_days(self, employee_email: str, days: float) -> float:
        """Record additional days used and persist immediately. Returns the new total."""
        key = employee_email.strip().lower()
        new_total = self.days_used(employee_email) + days
        self._usage[key] = new_total
        self.save()
        return new_total
