"""
State management for idempotency on new-hire provisioning.

Design choice: request-fingerprint, not a scanned-employee-ID set
--------------------------------------------------------------------
The Gusto-based version of this agent scanned for employees that already
existed in Gusto and tracked which Gusto employee UUIDs had been
provisioned. Deel has no equivalent "scan for pending hires" concept (see
config.py's module docstring), so this agent now takes one new hire's
details directly as input and creates them for real -- the same
one-request-per-run shape as the sibling pto-leave-request-agent, not a
scan-and-fan-out loop.

The idempotency question this agent answers is therefore "has THIS exact
new-hire request already been processed", not "does this Gusto ID already
exist in my records". Re-running the agent with the same
first_name+last_name+personal_email+start_date is almost always an accident
(a retried cron job, a re-run after a transient network error) rather than
an intentional second hire, so the guard is a straightforward fingerprint,
matching pto-leave-request-agent's state.py rationale exactly.

Deel's org_direct_employee_create has no delete/terminate tool anywhere in
its catalog (confirmed live, see connectors.py), so re-creating the same
person by accident is not just a wasted API call -- it is a real, permanent,
un-undoable duplicate record. This makes the idempotency guard here more
load-bearing than in agents whose write can be cleanly reversed or retried.

Per-step tracking (deel/workspace/notion/slack) is preserved from the
Gusto-based version's design: Google Workspace provisioning is expected to
fail or be skipped independently of Deel/Notion/Slack (see connectors.py
GoogleWorkspaceConnector and run_flow.py Step 3), so a hire whose Deel
record, Notion doc, and Slack welcome all succeeded but whose Workspace
account is still pending should not be silently marked "fully done" and
never revisited once Workspace provisioning becomes available.
"""

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Dict, Optional

_PID = os.getpid()

logger = logging.getLogger(__name__)

STEP_DEEL = "deel"
STEP_WORKSPACE = "workspace"
STEP_NOTION = "notion"
STEP_SLACK = "slack"
ALL_STEPS = (STEP_DEEL, STEP_WORKSPACE, STEP_NOTION, STEP_SLACK)


def compute_hire_fingerprint(first_name: str, last_name: str, personal_email: str, start_date: str) -> str:
    """Build a stable idempotency key for one create-mode new-hire request."""
    payload = {
        "first_name": first_name.strip().lower(),
        "last_name": last_name.strip().lower(),
        "personal_email": personal_email.strip().lower(),
        "start_date": start_date,
    }
    blob = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def compute_scan_fingerprint(deel_contract_id: str) -> str:
    """
    Build a stable idempotency key for a scan-mode hire, keyed on Deel's own
    real contract ID rather than a hash of name/email/date. Unlike create
    mode (where the record doesn't exist yet when the fingerprint is first
    computed), scan mode only ever sees hires that already have a real Deel
    contract -- so the contract ID itself is a simpler, equally stable key,
    and using it directly (instead of hashing name+email+date the way create
    mode does) means a name correction in Deel doesn't create a duplicate
    tracked record for the same real hire.
    """
    return f"scan:{deel_contract_id}"


class StateManager:
    """Tracks the outcome of each distinct new-hire request (by fingerprint), and which steps completed."""

    def __init__(self, state_file: Optional[Path] = None):
        if state_file is None:
            state_file = Path(__file__).parent / "state" / "provisioned_hires.json"
        self.state_file = state_file
        self._records: Dict[str, Dict] = {}
        self.load()

    def load(self) -> None:
        if self.state_file.exists():
            try:
                raw = json.loads(self.state_file.read_text())
                self._records = raw if isinstance(raw, dict) else {}
                logger.debug(f"Loaded provisioning state for {len(self._records)} hire request(s)")
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError, OSError):
                logger.warning("State file corrupted or unreadable, starting fresh")
                self._records = {}
        else:
            logger.debug("No state file found, starting fresh")
            self._records = {}

    def save(self) -> None:
        """
        Write the ledger atomically: re-read the current on-disk state and
        merge it with this process's in-memory entries first, then write to
        a temp file unique to this process, flush and fsync it, then fsync
        the containing directory too. Deel's real, verified lack of a
        delete/terminate tool for a direct employee (see connectors.py)
        makes losing this ledger's "already created" marker a genuinely
        expensive mistake -- a crash right after a real Deel creation that
        loses the marker risks a real, permanent duplicate hire on retry.
        """
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        on_disk: Dict[str, Dict] = {}
        if self.state_file.exists():
            try:
                raw = json.loads(self.state_file.read_text())
                on_disk = raw if isinstance(raw, dict) else {}
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError, OSError):
                pass
        merged = {**on_disk, **self._records}
        self._records = merged

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

    def get_record(self, fingerprint: str) -> Dict:
        return self._records.get(fingerprint, {})

    def completed_steps(self, fingerprint: str) -> set:
        return set(self.get_record(fingerprint).get("steps_done", []))

    def is_step_done(self, fingerprint: str, step: str) -> bool:
        return step in self.completed_steps(fingerprint)

    def is_fully_provisioned(self, fingerprint: str, required_steps=ALL_STEPS) -> bool:
        """True only if every step in `required_steps` has already succeeded for this hire."""
        done = self.completed_steps(fingerprint)
        return all(step in done for step in required_steps)

    def mark_step_done(self, fingerprint: str, step: str, detail: Optional[Dict] = None) -> None:
        """Record that `step` succeeded for this hire, and persist immediately."""
        record = self._records.setdefault(fingerprint, {"steps_done": [], "detail": {}})
        if step not in record["steps_done"]:
            record["steps_done"].append(step)
        if detail:
            record.setdefault("detail", {})[step] = detail
        self.save()
