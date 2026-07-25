"""
State management for change-detection on deal room syncs.

Tracks a content fingerprint of the last-synced deal context per opportunity,
so the agent only writes a new Drive comment when the underlying Salesforce
opportunity fields or the relevant Slack discussion have actually changed --
not just once per calendar day. This makes polling mode a real change
detector: leave it running continuously and it stays quiet until the deal
actually moves (stage change, amount update, new next-step, new relevant
Slack message), then syncs.
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def compute_deal_fingerprint(deal, slack_excerpts: List[str]) -> str:
    """
    Build a stable fingerprint over the meaningful, sync-worthy content:
    opportunity stage/amount/close date/next steps, plus the exact set of
    Slack excerpts captured this cycle. Sorted/rounded so run-to-run noise
    (dict ordering, float jitter) doesn't cause spurious "changes".
    """
    payload = {
        "stage": deal.stage,
        "amount": round(deal.amount) if deal.amount is not None else None,
        "close_date": deal.close_date,
        "next_step": (deal.next_step or "").strip(),
        "slack_excerpts": sorted(slack_excerpts),
    }
    blob = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class StateManager:
    """Tracks each opportunity's last-synced deal-context fingerprint for change detection."""

    def __init__(self, state_file: Path = None):
        if state_file is None:
            state_file = Path(__file__).parent / "state" / "synced_cycles.json"
        self.state_file = state_file
        self._last_fingerprint: Dict[str, str] = {}
        self.load()

    @staticmethod
    def _key(opportunity_id: str) -> str:
        return opportunity_id.strip().lower()

    def load(self) -> None:
        if self.state_file.exists():
            try:
                raw = json.loads(self.state_file.read_text())
                self._last_fingerprint = raw if isinstance(raw, dict) else {}
                logger.debug(f"Loaded fingerprint state for {len(self._last_fingerprint)} opportunity(ies)")
            except (json.JSONDecodeError, TypeError):
                logger.warning("State file corrupted, starting fresh")
                self._last_fingerprint = {}
        else:
            logger.debug("No state file found, starting fresh")
            self._last_fingerprint = {}

    def save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._last_fingerprint, indent=2, sort_keys=True))
        tmp.replace(self.state_file)  # atomic on POSIX

    def get_last_fingerprint(self, opportunity_id: str) -> Optional[str]:
        return self._last_fingerprint.get(self._key(opportunity_id))

    def has_changed(self, opportunity_id: str, fingerprint: str) -> bool:
        """True if this fingerprint differs from the last one synced for this opportunity."""
        return self.get_last_fingerprint(opportunity_id) != fingerprint

    def mark_synced(self, opportunity_id: str, fingerprint: str) -> None:
        self._last_fingerprint[self._key(opportunity_id)] = fingerprint
        self.save()
