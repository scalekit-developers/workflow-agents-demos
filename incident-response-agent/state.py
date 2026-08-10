"""
State management for idempotency on incident response runs.

Key shape and why
------------------
This agent's idempotency question is "has THIS specific alert already
triggered a full incident response" (an identity question), not "has the
underlying data changed" (a content-fingerprint question) -- because
re-running this agent for the same alert must NOT page on-call twice, open a
second Jira ticket, or post a duplicate Slack notification, even if the
agent is retried after a partial failure or invoked again by an
at-least-once alert delivery mechanism upstream.

A single run is keyed by the caller-supplied incident_key (e.g. an alert
fingerprint from the monitoring system, or a title+service combination if
the caller has no better key -- see run_flow.py). This same incident_key is
also passed to PagerDuty's own incident_create as its native deduplication
key, so even if this agent's local state file is ever lost or reset, a
retried run still cannot cause PagerDuty itself to open a second page for
the same alert; the local ledger's job is to also prevent the Jira ticket,
Confluence doc, and Slack notification from being duplicated, which
PagerDuty's own dedup does not cover.

Unlike a per-mention ledger (see the competitive-intelligence-briefing-agent
sibling repo), this ledger stores enough about the prior run's outcome
(the PagerDuty incident ID, Jira issue key, Confluence page ID, and Slack
message link) to report "already handled, here's what was created" on a
repeat run, rather than just silently no-op'ing.
"""

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def compute_incident_key(raw_key: str) -> str:
    """Build a stable, filesystem-safe idempotency key from a caller-supplied incident key."""
    normalized = raw_key.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class StateManager:
    """
    Tracks which incident_keys have already been fully handled (paged,
    ticketed, documented, notified), so a retried or duplicate-delivered
    alert never re-runs the whole response flow.
    """

    def __init__(self, state_file: Optional[Path] = None):
        if state_file is None:
            state_file = Path(__file__).parent / "state" / "handled_incidents.json"
        self.state_file = state_file
        self._handled: Dict[str, Dict] = {}
        self.load()

    def load(self) -> None:
        if self.state_file.exists():
            try:
                raw = json.loads(self.state_file.read_text())
                self._handled = raw if isinstance(raw, dict) else {}
                logger.debug(f"Loaded {len(self._handled)} previously-handled incident(s)")
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError, OSError):
                logger.warning("State file corrupted or unreadable, starting fresh")
                self._handled = {}
        else:
            logger.debug("No state file found, starting fresh")
            self._handled = {}

    def save(self) -> None:
        """
        Write the ledger atomically: write to a temp file, flush and fsync
        it so the data is actually durable on disk before the atomic
        rename, then replace the real state file. Without the fsync, a
        crash right after paging on-call could lose the "handled" marker
        despite the page having already gone out, risking a duplicate page
        on the next run.
        """
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        with open(tmp, "w") as f:
            f.write(json.dumps(self._handled, indent=2, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.state_file)  # atomic on POSIX

    def get_handled(self, key: str) -> Optional[Dict]:
        """Return the prior run's recorded outcome for this incident_key, or None if unhandled."""
        return self._handled.get(key)

    def mark_handled(self, key: str, outcome: Dict) -> None:
        self._handled[key] = outcome
        self.save()
