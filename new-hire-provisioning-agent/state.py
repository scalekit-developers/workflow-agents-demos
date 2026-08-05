"""
State management for tracking which Gusto employee IDs have already been
provisioned.

Design choice: processed-ID set, not a content fingerprint
------------------------------------------------------------
This agent's "has this record already been handled" question is fundamentally
different from revenue-forecast-commentary-agent's. That agent asks "has the
underlying pipeline DATA changed since I last posted", which is a genuine
content-diff problem (the same set of deals can be re-summarized every poll,
and only a change to deal counts/values/at-risk flags should trigger a new
Slack post) -- so it fingerprints the aggregate content.

This agent asks "has this specific new hire already been onboarded", which is
a one-time boolean per person, not a moving target to diff against. Gusto
employee records are not expected to meaningfully "change" in a way that
should re-trigger onboarding: once jane-doe-uuid has a Workspace account, a
Notion page, and a Slack welcome post, re-running the agent (or polling
continuously) must never re-provision her, REGARDLESS of whether her Gusto
record's start_date, title, or department gets edited afterward. Re-running
onboarding because a manager typo'd a job title and fixed it later would be a
bug, not a feature. So the right model is the ORIGINAL processed-ID-set
pattern (matching performance-review-collector-agent's early state.py, which
tracked processed (manager, review_period) cycles as a flat "already done"
set) rather than revenue-forecast-commentary-agent's content-fingerprint
pattern.

Each employee ID's state also tracks which of the three provisioning steps
(workspace, notion, slack) actually succeeded, not just a single "done"
boolean. This matters because Google Workspace provisioning is expected to
fail or be skipped independently of Notion/Slack (see connectors.py
GoogleWorkspaceConnector and run_flow.py Step 2): an employee whose Notion
doc and Slack welcome succeeded but whose Workspace account is still pending
should not be silently marked as "fully done" and never revisited once
Workspace provisioning becomes available. mark_step_done() records each step
independently; is_fully_provisioned() is only true once all three that were
attempted this run succeeded, so a partially-provisioned employee is
correctly re-surfaced by find_new_hires() on the next run to retry just the
missing step(s) (create_onboarding_page/send_welcome_message are themselves
idempotent/duplicate-safe as a second layer of protection, see connectors.py).
"""

import json
import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

STEP_WORKSPACE = "workspace"
STEP_NOTION = "notion"
STEP_SLACK = "slack"
ALL_STEPS = (STEP_WORKSPACE, STEP_NOTION, STEP_SLACK)


class StateManager:
    """Tracks which Gusto employee IDs have been provisioned, and which steps completed for each."""

    def __init__(self, state_file: Optional[Path] = None):
        if state_file is None:
            state_file = Path(__file__).parent / "state" / "provisioned_employees.json"
        self.state_file = state_file
        self._records: Dict[str, Dict] = {}
        self.load()

    @staticmethod
    def _key(employee_id: str) -> str:
        return str(employee_id).strip()

    def load(self) -> None:
        if self.state_file.exists():
            try:
                raw = json.loads(self.state_file.read_text())
                self._records = raw if isinstance(raw, dict) else {}
                logger.debug(f"Loaded provisioning state for {len(self._records)} employee(s)")
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError, FileNotFoundError):
                logger.warning("State file corrupted or unreadable, starting fresh")
                self._records = {}
        else:
            logger.debug("No state file found, starting fresh")
            self._records = {}

    def save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._records, indent=2, sort_keys=True))
        tmp.replace(self.state_file)  # atomic on POSIX

    def get_record(self, employee_id: str) -> Dict:
        return self._records.get(self._key(employee_id), {})

    def completed_steps(self, employee_id: str) -> set:
        return set(self.get_record(employee_id).get("steps_done", []))

    def is_step_done(self, employee_id: str, step: str) -> bool:
        return step in self.completed_steps(employee_id)

    def is_fully_provisioned(self, employee_id: str, required_steps=ALL_STEPS) -> bool:
        """True only if every step in `required_steps` has already succeeded for this employee."""
        done = self.completed_steps(employee_id)
        return all(step in done for step in required_steps)

    def mark_step_done(self, employee_id: str, step: str, detail: Optional[Dict] = None) -> None:
        """Record that `step` succeeded for this employee, and persist immediately."""
        key = self._key(employee_id)
        record = self._records.setdefault(key, {"steps_done": [], "detail": {}})
        if step not in record["steps_done"]:
            record["steps_done"].append(step)
        if detail:
            record.setdefault("detail", {})[step] = detail
        self.save()

    def all_provisioned_ids(self) -> set:
        return set(self._records.keys())
