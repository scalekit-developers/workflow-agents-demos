"""
State management for idempotency on PTO submissions.

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
treated as the same request: if the first one already completed the Deel
submission, Google Calendar block, and Slack notification, the second run
skips re-doing that work rather than creating a duplicate Deel request or
double-DMing the manager. This also makes "submit once" and "process a small
pending queue" the same underlying operation: each entry in the request
ledger is one request, keyed by its own fingerprint, so POLLING_MODE (see
run_flow.py) can safely re-check the same request on an interval without
side effects once it's marked complete.

There is no separate local balance ledger: Deel's
deelmcp_timeoff_entitlement_list is a real, authoritative balance source,
read fresh at request time (see aggregator.py), so there is nothing for a
local running total to track.
"""

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Dict, Optional

_PID = os.getpid()

logger = logging.getLogger(__name__)


def compute_request_fingerprint(employee_email: str, pto_type: str, start_date: str, end_date: str) -> str:
    """
    Build a stable idempotency key for one leave request.

    This fingerprint is keyed on the request's identity, not its content,
    since the "content" of a PTO request (who, what type, which dates) IS
    its identity -- there is nothing else to compare against.
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
    re-run of the same request doesn't re-submit to Deel, re-block the
    calendar, or re-send the Slack DM for work that already completed.
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
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError, OSError):
                logger.warning("State file corrupted or unreadable, starting fresh")
                self._requests = {}
        else:
            logger.debug("No state file found, starting fresh")
            self._requests = {}

    def save(self) -> None:
        """
        Write the ledger atomically: re-read the current on-disk state and
        merge it with this process's in-memory entries first (so a second
        concurrent run touching a different request fingerprint doesn't
        silently lose its write if this process saves last), then write to a
        temp file unique to this process, flush and fsync it so the data is
        actually durable on disk before the atomic rename, then fsync the
        containing directory too -- a rename's directory-entry update is a
        separate durability guarantee from the file's own contents being
        flushed, and without it a crash immediately after the rename could
        still lose the just-written "completed" marker despite the request
        having already reached Deel, risking a duplicate submission on the
        next run.
        """
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        on_disk: Dict[str, Dict] = {}
        if self.state_file.exists():
            try:
                raw = json.loads(self.state_file.read_text())
                on_disk = raw if isinstance(raw, dict) else {}
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError, OSError):
                pass  # corrupted/unreadable on-disk state loses to this process's in-memory view, same as load()'s own fallback
        merged = {**on_disk, **self._requests}
        self._requests = merged

        tmp = self.state_file.with_suffix(f".tmp.{_PID}")
        with open(tmp, "w") as f:
            f.write(json.dumps(merged, indent=2, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.state_file)  # atomic on POSIX

        dir_fd = os.open(str(self.state_file.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

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
        steps actually ran (e.g. Deel submission succeeded but Slack failed).
        """
        entry = self._requests.setdefault(fingerprint, {})
        entry.update(fields)
        self.save()

    def mark_completed(self, fingerprint: str, **fields) -> None:
        self.mark_step(fingerprint, status="completed", **fields)
