"""
State management for idempotency: detect an exact-duplicate resubmission of
the same payroll/bank-detail change (e.g. someone submits the same form
twice, or a retry after a network blip re-sends an already-processed
request), so the agent does not resubmit to Gusto and does not double-log to
Google Sheets or send a duplicate Slack confirmation.

Design choice: hash the new value, never store it in plaintext.

The idempotency key is a fingerprint of (employee identifier, change type,
SHA-256 hash of the new value) -- NOT the plaintext new value itself. This
mirrors the PII-safety reasoning in logging_config.py: a bank account number
or routing number is exactly the kind of value that must never be written to
disk in recoverable form as a side effect of an unrelated feature (here,
duplicate-submission detection). Hashing keeps the fingerprinting property
we need (byte-identical inputs produce byte-identical, comparable keys) while
making the state file itself safe to inspect, back up, or accidentally leak
without exposing a real bank/routing number. A salted or keyed hash was
considered and rejected: this is a same-process exact-match check, not a
password store, so a well-distributed unsalted SHA-256 over
(employee_email, change_type, new_value) is sufficient -- the goal is
collision-avoidance for practical duplicate detection, not resistance to a
dedicated offline attacker with a rainbow table of guessed bank details (an
attacker who can already read this state file has access to systems where
plaintext values are a bigger concern than this hash).

Exact-match design vs. a broader "already changed this field recently"
cooldown: this agent treats idempotency narrowly, as "was this EXACT change
(same employee, same field, same new value) already successfully submitted",
not "was this field changed recently at all" (that broader cooldown is a
business-logic eligibility concern, handled separately in aggregator.py's
eligibility gate, not here). A duplicate-form-submission scenario is exactly
"same employee resubmits the identical change", so an exact-match hash key is
the right fit: it deliberately does NOT block a legitimate follow-up change
to a *different* new value (e.g. employee corrects a typo'd account number
in a second submission), only a byte-identical resend of a change already
processed successfully.
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def compute_change_fingerprint(employee_email: str, change_type: str, new_value: str) -> str:
    """
    Build a stable idempotency key for a single payroll-change request.

    Hashes (employee_email, change_type, new_value) together so the SAME new
    value submitted for a DIFFERENT field, or the same field for a DIFFERENT
    employee, is correctly treated as a distinct change (not a duplicate).
    """
    payload = f"{employee_email.strip().lower()}::{change_type.strip().lower()}::{new_value}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class StateManager:
    """
    Tracks successfully-submitted payroll changes, keyed by change
    fingerprint (see compute_change_fingerprint), so an exact-duplicate
    resubmission is detected before any Gusto write, Sheets log, or Slack
    confirmation is attempted a second time.
    """

    def __init__(self, state_file: Optional[Path] = None):
        if state_file is None:
            state_file = Path(__file__).parent / "state" / "processed_changes.json"
        self.state_file = state_file
        self._processed: Dict[str, Dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        if self.state_file.exists():
            try:
                raw = json.loads(self.state_file.read_text())
                self._processed = raw if isinstance(raw, dict) else {}
                logger.debug(f"Loaded idempotency state for {len(self._processed)} processed change(s)")
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError, FileNotFoundError):
                logger.warning("State file corrupted or unreadable, starting fresh")
                self._processed = {}
        else:
            logger.debug("No state file found, starting fresh")
            self._processed = {}

    def save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._processed, indent=2, sort_keys=True))
        tmp.replace(self.state_file)  # atomic on POSIX

    def already_processed(self, fingerprint: str) -> bool:
        """True if this exact (employee, change_type, new_value) was already submitted successfully."""
        return fingerprint in self._processed

    def get_record(self, fingerprint: str) -> Optional[Dict[str, Any]]:
        return self._processed.get(fingerprint)

    def mark_processed(
        self,
        fingerprint: str,
        employee_email: str,
        change_type: str,
        masked_value: str,
        submitted_at: str,
    ) -> None:
        """
        Record a successfully-submitted change. Stores only the MASKED value
        (e.g. "****1234"), never the plaintext new value and never the raw
        hash input, so the state file itself carries no recoverable PII.
        """
        self._processed[fingerprint] = {
            "employee_email": employee_email,
            "change_type": change_type,
            "masked_value": masked_value,
            "submitted_at": submitted_at,
        }
        self.save()
